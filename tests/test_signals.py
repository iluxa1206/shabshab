"""Вкладка СИГНАЛЫ: ядро скринера, хранение фильтров, лента, анти-спам."""
import asyncio

import pytest

from services import screener_core as core
from services import signals
from services.portfolio_db import _connect, _lock, init_db

USER = "signals-test@example.com"


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    """Каждый тест — на своей пустой БД во временном каталоге.

    Раньше фикстура работала прямо в боевой data/portfolio.db (единственный
    тест-модуль без подмены пути) и подчищала за собой DELETE'ами. Два изъяна:
    фиктивные ISIN оседали в signal_events при падении теста, а зачистка
    signal_state шла БЕЗ фильтра по user_email — то есть удаляла осиротевшие
    строки состояния всех пользователей, о которых тест ничего не знает.

    Подменяем DB_PATH атрибутом модуля, а не reload'ом: _connect() читает
    глобал в момент вызова, поэтому подмена видна и signals, который взял
    _connect импортом на уровне модуля."""
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "portfolio.db")
    init_db()
    yield


def _market():
    uni = [
        {"isin": "RU000A0000A1", "name": "Газпром 1", "rating": "AA",
         "emitter_name": "Газпром капитал", "maturity_date": "2029-08-11"},
        {"isin": "RU000A0000B2", "name": "Мелкий БО", "rating": "BBB",
         "emitter_name": "Мелкая контора", "maturity_date": "2027-02-10"},
        {"isin": "RU000A0000C3", "name": "ВЭБ 3", "rating": "AAA",
         "emitter_name": "ВЭБ.РФ", "maturity_date": "2031-08-11"},
    ]
    # yoi_slope — производная Y-IDX по цене (бп на 1 пп), нужна для спреда по VWAP
    metrics = {
        "RU000A0000A1": {"yoi_ask": 280.0, "yoi_bid": 300.0, "ask": 100.2, "bid": 99.9,
                         "face_px": 1000.0, "accrued_settle": 0.0, "yoi_slope": -100.0},
        "RU000A0000B2": {"yoi_ask": 400.0, "yoi_bid": 420.0, "ask": 99.0, "bid": 98.5,
                         "face_px": 1000.0, "accrued_settle": 0.0, "yoi_slope": -100.0},
        "RU000A0000C3": {"yoi_ask": 180.0, "yoi_bid": 195.0, "ask": 100.0, "bid": 99.8,
                         "face_px": 1000.0, "accrued_settle": 0.0, "yoi_slope": -100.0},
    }
    depth = {"RU000A0000A1": {"a": [[100.2, 3000], [100.4, 5000]], "b": [[99.9, 100]]},
             "RU000A0000B2": {"a": [[99.0, 50]], "b": []}}
    return uni, metrics, depth


def test_core_shared_with_tg():
    assert signals.normalize_params is core.normalize_params
    assert signals.evaluate is core.evaluate


def test_evaluate_range_selectors_money():
    uni, metrics, depth = _market()
    p = core.normalize_params({"spread_min": 250, "spread_max": 450})
    assert [m["isin"] for m in core.evaluate(p, uni, metrics, depth)] \
        == ["RU000A0000B2", "RU000A0000A1"]

    p2 = core.normalize_params({"spread_min": 100, "ratings": ["AA"], "emitters": ["ВЭБ.РФ"]})
    assert sorted(m["isin"] for m in core.evaluate(p2, uni, metrics, depth)) \
        == ["RU000A0000A1", "RU000A0000C3"]

    p3 = core.normalize_params({"spread_min": 250, "min_money_rub": 1e6})
    assert [m["isin"] for m in core.evaluate(p3, uni, metrics, depth)] == ["RU000A0000A1"]


def test_evaluate_bid_side():
    uni, metrics, depth = _market()
    p = core.normalize_params({"spread_min": 100, "side": "bid", "isins": ["RU000A0000A1"]})
    m = core.evaluate(p, uni, metrics, depth)[0]
    assert m["val_bps"] == 300.0 and m["price"] == 99.9
    assert m["money_rub"] == pytest.approx(99.9 / 100 * 1000 * 100)


def test_crud_and_isolation():
    f = signals.create(USER, "мой сигнал", {"spread_min": 250}, change_pct=5)
    assert f["enabled"] and f["sound"] and f["desktop"]
    assert f["params"]["side"] == "ask"

    assert len(signals.list_for_user(USER)) == 1
    assert signals.list_for_user("other@example.com") == []
    assert signals.update("other@example.com", f["id"], enabled=False) is None
    assert signals.delete("other@example.com", f["id"]) is False

    upd = signals.update(USER, f["id"], enabled=False, sound=False, change_pct=20)
    assert upd["enabled"] is False and upd["sound"] is False and upd["change_pct"] == 20
    assert signals.delete(USER, f["id"]) is True


def test_create_validates():
    with pytest.raises(signals.FilterError):
        signals.create(USER, "", {"spread_min": 250})
    with pytest.raises(signals.FilterError):
        signals.create(USER, "x", {})
    with pytest.raises(signals.FilterError):
        signals.create(USER, "x", {"spread_min": 250}, change_pct=0)


def test_run_cycle_pushes_to_owner(monkeypatch):
    import asyncio
    uni, metrics, depth = _market()

    async def fake_snapshot():
        return uni, metrics, depth
    monkeypatch.setattr(signals, "market_snapshot", fake_snapshot)
    # цикл считает спред верифицированным путём (reprice) — в юните подменяем
    # его на наклон по фиктивным метрикам, иначе контекста бумаги просто нет
    async def fake_warm(isins):
        return 0
    monkeypatch.setattr(signals, "warm_exact_ctx", fake_warm)
    monkeypatch.setattr(core, "exact_y_idx",
                        lambda i, px: core.y_idx_at(metrics.get(i) or {}, px, "ask"))

    sent = []
    from api.routes import ws as wsmod

    async def fake_broadcast(email, payload):
        sent.append((email, payload))
    monkeypatch.setattr(wsmod.manager, "broadcast_signal", fake_broadcast)

    f = signals.create(USER, "цикл", {"spread_min": 250, "side": "ask"})
    assert asyncio.run(signals.run_cycle()) >= 1
    mine = [s for s in sent if s[0] == USER]
    assert len(mine) == 1
    payload = mine[0][1]
    assert payload["filter_name"] == "цикл" and payload["side"] == "ask"
    assert {m["isin"] for m in payload["matches"]} == {"RU000A0000A1", "RU000A0000B2"}

    assert all(m["reason"] == "new" for m in payload["matches"])

    # второй тик без движения рынка — тишина
    sent.clear()
    asyncio.run(signals.run_cycle())
    assert [s for s in sent if s[0] == USER] == []
    signals.delete(USER, f["id"])


# --- суборды и срок до погашения ---

def test_is_subord_by_name():
    assert core.is_subord({"name": "ВТБСУБ1-12"})
    assert core.is_subord({"name": "ВТБСУБТ1Р2"})
    assert core.is_subord({"name": "Банк Т1"})
    assert core.is_subord({"name": "Перп выпуск"})
    # обычные бумаги не должны ловиться: Т1 внутри слова/номера — не маркер
    assert not core.is_subord({"name": "Газпром капитал БО-002P-08"})
    assert not core.is_subord({"name": "МТС 2Р-11"})
    assert not core.is_subord({"name": "ИКС5Фин3P4"})
    assert not core.is_subord({"name": ""})


def test_hide_subord_filters():
    from datetime import date as _d
    uni, metrics, depth = _market()
    uni.append({"isin": "RU000A0000S9", "name": "ВТБСУБ1-12", "rating": "AAA",
                "emitter_name": "Банк ВТБ ПАО", "maturity_date": "2027-03-29"})
    metrics["RU000A0000S9"] = {"yoi_ask": 9000.0, "yoi_bid": 9200.0, "ask": 68.5,
                               "bid": 68.0, "face_px": 1000.0}
    today = _d(2026, 8, 11)

    p = core.normalize_params({"spread_min": 150})
    assert "RU000A0000S9" in [m["isin"] for m in core.evaluate(p, uni, metrics, depth, today)]

    p2 = core.normalize_params({"spread_min": 150, "hide_subord": True})
    got = [m["isin"] for m in core.evaluate(p2, uni, metrics, depth, today)]
    assert "RU000A0000S9" not in got and "RU000A0000A1" in got


def test_years_range_filters():
    from datetime import date as _d
    uni, metrics, depth = _market()
    today = _d(2026, 8, 11)

    # A1 ~3 года, B2 ~0.5, C3 ~5
    p = core.normalize_params({"spread_min": 100, "years_max": 1})
    assert [m["isin"] for m in core.evaluate(p, uni, metrics, depth, today)] == ["RU000A0000B2"]

    p2 = core.normalize_params({"spread_min": 100, "years_min": 4})
    assert [m["isin"] for m in core.evaluate(p2, uni, metrics, depth, today)] == ["RU000A0000C3"]

    p3 = core.normalize_params({"spread_min": 100, "years_min": 1, "years_max": 4})
    assert [m["isin"] for m in core.evaluate(p3, uni, metrics, depth, today)] == ["RU000A0000A1"]

    # срок возвращается в матче
    m = core.evaluate(p3, uni, metrics, depth, today)[0]
    assert 2.9 < m["years"] < 3.1


def test_years_without_maturity_excluded():
    from datetime import date as _d
    uni, metrics, depth = _market()
    uni.append({"isin": "RU000A0000P8", "name": "Бессрочный", "rating": "AAA",
                "emitter_name": "Кто-то", "maturity_date": None})
    metrics["RU000A0000P8"] = {"yoi_ask": 500.0, "yoi_bid": 520.0, "ask": 90.0,
                               "bid": 89.0, "face_px": 1000.0}
    today = _d(2026, 8, 11)

    # без ограничения срока бумага видна
    p = core.normalize_params({"spread_min": 100})
    assert "RU000A0000P8" in [m["isin"] for m in core.evaluate(p, uni, metrics, depth, today)]
    # с ограничением — нет: срок неизвестен, «до 2 лет» её пустить не может
    p2 = core.normalize_params({"spread_min": 100, "years_max": 2})
    assert "RU000A0000P8" not in [m["isin"] for m in core.evaluate(p2, uni, metrics, depth, today)]


def test_years_range_validation():
    with pytest.raises(core.FilterError):
        core.normalize_params({"spread_min": 100, "years_min": 5, "years_max": 2})
    with pytest.raises(core.FilterError):
        core.normalize_params({"spread_min": 100, "years_min": -1})


# --- VWAP на объём: цена и спред по набранному тикету ---

def test_vwap_price_and_spread_by_volume():
    uni, metrics, depth = _market()
    # 4 млн ₽ по A1: 3000 бумаг по 100.2 = 3.006 млн, остаток берём с 100.4
    p = core.normalize_params({"spread_min": 100, "min_money_rub": 4e6,
                               "isins": ["RU000A0000A1"]})
    m = core.evaluate(p, uni, metrics, depth)[0]
    assert m["levels"] == 2 and m["partial"] is False
    assert m["money_rub"] == pytest.approx(4e6)
    # средневзвес между 100.2 и 100.4, ближе к 100.2
    assert 100.2 < m["price"] < 100.4
    # спред пересчитан к этой цене наклоном: хуже, чем 280 у верха стакана
    assert m["val_bps"] < 280.0
    assert m["val_bps"] == pytest.approx(280.0 + (m["price"] - 100.2) * -100.0, abs=0.2)


def test_vwap_single_level_uses_exact_top_spread():
    uni, metrics, depth = _market()
    metrics["RU000A0000A1"].pop("yoi_slope")     # наклона нет
    p = core.normalize_params({"spread_min": 100, "min_money_rub": 1e6,
                               "isins": ["RU000A0000A1"]})
    m = core.evaluate(p, uni, metrics, depth)[0]
    # набор уложился в верхний уровень → спред верха точен, а не приближение
    assert m["levels"] == 1 and m["val_bps"] == 280.0


def test_vwap_rejects_when_book_too_thin():
    uni, metrics, depth = _market()
    # у B2 на оффере всего 49.5 тыс ₽ — тикет на 1 млн не собрать
    p = core.normalize_params({"spread_min": 100, "min_money_rub": 1e6,
                               "isins": ["RU000A0000B2"]})
    assert core.evaluate(p, uni, metrics, depth) == []


def test_vwap_partial_within_tolerance_passes():
    uni, metrics, depth = _market()
    # книги A1 хватает на 8.026 млн; просим 8.5 млн — добрали 94% ≥ VOL_TOL
    p = core.normalize_params({"spread_min": 100, "min_money_rub": 8.5e6,
                               "isins": ["RU000A0000A1"]})
    m = core.evaluate(p, uni, metrics, depth)[0]
    assert m["partial"] is True and m["money_rub"] == pytest.approx(8_026_000)

    # просим 10 млн — добрали 80% < VOL_TOL, тикет не собрать
    p2 = core.normalize_params({"spread_min": 100, "min_money_rub": 10e6,
                                "isins": ["RU000A0000A1"]})
    assert core.evaluate(p2, uni, metrics, depth) == []


def test_dirty_money_includes_accrued():
    uni, metrics, depth = _market()
    metrics["RU000A0000A1"]["accrued_settle"] = 50.0     # НКД 50 ₽ на бумагу
    p = core.normalize_params({"spread_min": 100, "isins": ["RU000A0000A1"]})
    m = core.evaluate(p, uni, metrics, depth)[0]
    # 3000×(1000×1.002+50) + 5000×(1000×1.004+50)
    assert m["money_rub"] == pytest.approx(3000 * 1052 + 5000 * 1054)


# --- событийная модель ---

def test_events_new_then_silence_then_change():
    uni, metrics, depth = _market()
    f = signals.create(USER, "события", {"spread_min": 100, "isins": ["RU000A0000A1"]},
                       change_pct=10)
    p = f["params"]

    ms = core.evaluate(p, uni, metrics, depth)
    ev = signals.detect_events(f["id"], USER, "ask", 10, ms, None)
    assert [e["reason"] for e in ev] == ["new"]

    # ничего не изменилось — событий нет
    assert signals.detect_events(f["id"], USER, "ask", 10, ms, None) == []

    # цена уехала на 0.05 п.п. — ниже порога полфигуры, молчим
    metrics["RU000A0000A1"]["ask"] = 100.25
    ms2 = core.evaluate(p, uni, metrics, depth)
    assert signals.detect_events(f["id"], USER, "ask", 10, ms2, None) == []

    # спред уехал на 60 бп — выше порога SPREAD_REPEAT_BPS, событие
    metrics["RU000A0000A1"]["yoi_ask"] = 340.0
    ms3 = core.evaluate(p, uni, metrics, depth)
    ev3 = signals.detect_events(f["id"], USER, "ask", 10, ms3, None)
    assert [e["reason"] for e in ev3] == ["spread"]
    assert ev3[0]["prev_val_bps"] == 280.0

    signals.delete(USER, f["id"])


def _stub_exact_map(monkeypatch, metrics, side="ask"):
    """Юниты про ЛОГИКУ событий, а не про расчёт: контекста пересчёта у фиктивных
    бумаг нет, поэтому точную карту спредов подменяем наклоном по тем же
    метрикам. Без подмены money_in_spread молчит и «объём изменился» не ловится."""
    monkeypatch.setattr(core, "exact_y_idx_map",
                        lambda isin, pxs: {round(float(p), core._EXACT_PX_DIGITS):
                                           core.y_idx_at(metrics.get(isin) or {}, p, side)
                                           for p in pxs if p is not None})


def test_events_money_change_and_leaving_set(monkeypatch):
    uni, metrics, depth = _market()
    _stub_exact_map(monkeypatch, metrics)
    f = signals.create(USER, "объём", {"spread_min": 100, "isins": ["RU000A0000A1"]},
                       change_pct=10)
    p = f["params"]
    signals.detect_events(f["id"], USER, "ask", 10, core.evaluate(p, uni, metrics, depth), None)

    # объём в стакане вырос вдвое — событие money
    depth["RU000A0000A1"]["a"] = [[100.2, 6000], [100.4, 5000]]
    ev = signals.detect_events(f["id"], USER, "ask", 10,
                               core.evaluate(p, uni, metrics, depth), None)
    assert [e["reason"] for e in ev] == ["money"]

    # Короткий выход из набора и возврат — НЕ «заявка»: стакан дрожит у границы
    # фильтра, и каждое возвращение звонило бы заново (см. RETURN_GRACE_MIN).
    signals.detect_events(f["id"], USER, "ask", 10, [], None)
    ev2 = signals.detect_events(f["id"], USER, "ask", 10,
                                core.evaluate(p, uni, metrics, depth), None)
    assert ev2 == []

    signals.delete(USER, f["id"])


def test_return_after_grace_is_new_again(monkeypatch):
    """Бумага, которой не было в наборе дольше срока памяти, возвращается как
    «заявка» — иначе реально новую заявку после долгой паузы не отличить."""
    uni, metrics, depth = _market()
    f = signals.create(USER, "возврат", {"spread_min": 100, "isins": ["RU000A0000A1"]},
                       change_pct=10)
    p = f["params"]
    ms = core.evaluate(p, uni, metrics, depth)
    assert [e["reason"] for e in signals.detect_events(f["id"], USER, "ask", 10, ms, None)] \
        == ["new"]

    monkeypatch.setattr(signals, "RETURN_GRACE_MIN", 0.0)
    signals.detect_events(f["id"], USER, "ask", 10, [], None)   # ушла и забыта
    ev = signals.detect_events(f["id"], USER, "ask", 10, ms, None)
    assert [e["reason"] for e in ev] == ["new"]

    signals.delete(USER, f["id"])


def test_cooldown_mutes_same_reason_only(monkeypatch):
    """Повтор ТОЙ ЖЕ причины в пределах кулдауна молчит, другая причина проходит."""
    uni, metrics, depth = _market()
    _stub_exact_map(monkeypatch, metrics)
    f = signals.create(USER, "кулдаун", {"spread_min": 100, "isins": ["RU000A0000A1"]},
                       change_pct=10)
    p = f["params"]
    signals.detect_events(f["id"], USER, "ask", 10, core.evaluate(p, uni, metrics, depth), None)

    metrics["RU000A0000A1"]["yoi_ask"] = 340.0
    ev = signals.detect_events(f["id"], USER, "ask", 10,
                               core.evaluate(p, uni, metrics, depth), None)
    assert [e["reason"] for e in ev] == ["spread"]

    # ещё 60 бп сразу следом — та же причина, кулдаун молчит
    metrics["RU000A0000A1"]["yoi_ask"] = 400.0
    assert signals.detect_events(f["id"], USER, "ask", 10,
                                 core.evaluate(p, uni, metrics, depth), None) == []

    # объём вырос — другая причина, проходит без ожидания
    depth["RU000A0000A1"]["a"] = [[100.2, 6000], [100.4, 5000]]
    ev2 = signals.detect_events(f["id"], USER, "ask", 10,
                                core.evaluate(p, uni, metrics, depth), None)
    assert [e["reason"] for e in ev2] == ["money"]

    monkeypatch.setattr(signals, "COOLDOWN_MIN", 0.0)
    metrics["RU000A0000A1"]["yoi_ask"] = 460.0
    ev3 = signals.detect_events(f["id"], USER, "ask", 10,
                                core.evaluate(p, uni, metrics, depth), None)
    assert [e["reason"] for e in ev3] == ["spread"]

    signals.delete(USER, f["id"])


def test_events_feed_and_unseen_counter():
    uni, metrics, depth = _market()
    f = signals.create(USER, "лента", {"spread_min": 100, "isins": ["RU000A0000A1"]})
    signals.detect_events(f["id"], USER, "ask", 10,
                          core.evaluate(f["params"], uni, metrics, depth), 4e6)

    feed = signals.events_for_user(USER)
    assert len(feed) == 1 and feed[0]["filter_name"] == "лента"
    assert feed[0]["reason"] == "new" and feed[0]["want_money_rub"] == 4e6
    assert signals.unseen_count(USER) == 1
    assert signals.mark_seen(USER) == 1 and signals.unseen_count(USER) == 0

    # чистка ленты НЕ воскрешает бумаги как новые (состояние живёт отдельно)
    signals.clear_events(USER)
    assert signals.events_for_user(USER) == []
    assert signals.detect_events(f["id"], USER, "ask", 10,
                                 core.evaluate(f["params"], uni, metrics, depth), None) == []
    signals.delete(USER, f["id"])


def test_static_candidates_split():
    uni, _metrics, _depth = _market()
    p = core.normalize_params({"spread_min": 100, "ratings": ["AA"]})
    cands = core.static_candidates(p, uni)
    assert [c["isin"] for c in cands] == ["RU000A0000A1"]
    assert cands[0]["_years"] is not None


# --- маршруты: именованные пути не должны съедаться параметрическим /{fid} ---

def test_named_routes_win_over_param_route():
    from api.routes import signals as route
    paths = [(r.path, sorted(r.methods - {"HEAD", "OPTIONS"}))
             for r in route.router.routes]
    named = [i for i, (p, _) in enumerate(paths) if "{" not in p]
    param = [i for i, (p, _) in enumerate(paths) if "{" in p]
    assert named and param
    # каждый именованный маршрут объявлен РАНЬШЕ любого параметрического,
    # иначе DELETE /events попадал бы в DELETE /{fid} и падал с 422
    assert max(named) < min(param)


# --- крупные заявки: режим объёма single vs book, спред необязателен ---

def _big_small():
    uni = [{"isin": "RU000A0000A1", "name": "ААА-бонд", "rating": "AAA",
            "emitter_name": "Эмитент", "maturity_date": "2029-01-01"}]
    metrics = {"RU000A0000A1": {"yoi_ask": 300.0, "yoi_bid": 310.0, "ask": 100.0,
                                "bid": 99.5, "face_px": 1000.0, "accrued_settle": 0.0,
                                "yoi_slope": -100.0}}
    # книга А: сумма 6 млн, но мелкими заявками по 600 тыс
    small = {"RU000A0000A1": {"a": [[100.0 + i * 0.01, 600] for i in range(10)], "b": []}}
    # книга Б: одна заявка на 6 млн
    big = {"RU000A0000A1": {"a": [[100.0, 100], [100.05, 6000]], "b": []}}
    return uni, metrics, small, big


def test_spread_is_optional_when_volume_set():
    p = core.normalize_params({"ratings": ["AAA"], "min_money_rub": 5e6})
    assert p["spread_min"] is None and p["spread_max"] is None
    # но совсем без условий фильтр не имеет смысла
    with pytest.raises(core.FilterError):
        core.normalize_params({"ratings": ["AAA"]})


def test_single_mode_needs_one_big_order():
    uni, metrics, small, big = _big_small()
    p = core.normalize_params({"ratings": ["AAA"], "min_money_rub": 5e6,
                               "money_mode": "single"})
    # двадцать мелких заявок на ту же сумму — НЕ крупная заявка
    assert core.evaluate(p, uni, metrics, small) == []
    m = core.evaluate(p, uni, metrics, big)[0]
    assert m["levels"] == 1 and m["money_rub"] == pytest.approx(6_003_000)
    assert m["price"] == 100.05 and m["single_px"] == 100.05
    # спред пересчитан к цене заявки, а не взят с верха стакана
    assert m["val_bps"] == pytest.approx(300.0 + (100.05 - 100.0) * -100.0, abs=0.2)


def test_book_mode_accepts_many_small_orders():
    uni, metrics, small, big = _big_small()
    p = core.normalize_params({"ratings": ["AAA"], "min_money_rub": 5e6,
                               "money_mode": "book"})
    m = core.evaluate(p, uni, metrics, small)[0]
    assert m["levels"] > 1 and m["single_px"] is None
    assert m["money_rub"] == pytest.approx(5e6)


def test_single_mode_no_spread_bounds_keeps_bond_without_slope():
    uni, metrics, _small, big = _big_small()
    metrics["RU000A0000A1"].pop("yoi_slope")      # наклон не посчитался
    p = core.normalize_params({"ratings": ["AAA"], "min_money_rub": 5e6,
                               "money_mode": "single"})
    # спред не задан условием → бумага не должна теряться из-за пустого Y-IDX
    m = core.evaluate(p, uni, metrics, big)[0]
    assert m["money_rub"] > 5e6 and m["val_bps"] is None


def test_money_mode_validated():
    with pytest.raises(core.FilterError):
        core.normalize_params({"min_money_rub": 1e6, "money_mode": "мусор"})


def test_delete_all_by_kind_keeps_other_column():
    """«Удалить все» в одной колонке UI не должно снести вторую."""
    book = signals.create(USER, "стакан", {"side": "ask", "spread_min": 100})
    blk = signals.create(USER, "блоки", {"min_value_rub": 5_000_000}, kind="block")
    assert signals.delete_all(USER, kind="block") == 1
    left = signals.list_for_user(USER)
    assert [f["id"] for f in left] == [book["id"]]
    assert signals.get(blk["id"]) is None
    assert signals.delete_all(USER, kind="book") == 1
    assert signals.list_for_user(USER) == []


# --- ОФЗ / корп: один переключатель на оба вида сигналов ---

def _ofz_market():
    """К рынку из _market() добавлена ОФЗ-ПК — распознаётся по имени/эмитенту."""
    uni, metrics, depth = _market()
    uni.append({"isin": "RU000A0000O7", "name": "ОФЗ 29014", "rating": "AAA",
                "emitter_name": "Минфин России", "maturity_date": "2030-08-11"})
    metrics["RU000A0000O7"] = {"yoi_ask": 150.0, "yoi_bid": 165.0, "ask": 99.5,
                               "bid": 99.3, "face_px": 1000.0, "accrued_settle": 0.0,
                               "yoi_slope": -100.0}
    return uni, metrics, depth


def test_issuer_switch_book_filter():
    uni, metrics, depth = _ofz_market()
    p = core.normalize_params({"spread_min": 100, "issuer": "ofz"})
    assert [m["isin"] for m in core.evaluate(p, uni, metrics, depth)] == ["RU000A0000O7"]

    p2 = core.normalize_params({"spread_min": 100, "issuer": "corp"})
    assert "RU000A0000O7" not in [m["isin"] for m in core.evaluate(p2, uni, metrics, depth)]

    p3 = core.normalize_params({"spread_min": 100})
    assert p3["issuer"] == "all"
    assert "RU000A0000O7" in [m["isin"] for m in core.evaluate(p3, uni, metrics, depth)]


def test_is_ofz_by_secid_and_name_not_by_subfederal():
    assert core.is_ofz({"secid": "SU26248RMFS3", "name": "ОФЗ 26248"})
    assert core.is_ofz({"name": "ОФЗ 29014"})
    assert core.is_ofz({"name": "Что-то", "emitter_name": "Минфин России"})
    # субфедерал идёт с «Минфин <области>» — это НЕ ОФЗ
    assert not core.is_ofz({"secid": "RU000A0JX0J2", "name": "Амур 24001",
                            "emitter_name": "Минфин Амурской обл."})
    assert not core.is_ofz({"secid": "RU000A10AU99", "name": "СУЭК 1Р5"})


def test_issuer_validation():
    with pytest.raises(core.FilterError):
        core.normalize_params({"spread_min": 100, "issuer": "муни"})


# --- блок-фильтр: спред, срок, ОФЗ/корп ---

def _blk(**kw):
    return core.normalize_block_params({"min_value_rub": 5_000_000, **kw})


def _trade(**kw):
    return {"isin": "RU000A0000A1", "secid": "RU000A0000A1", "value": 6_000_000,
            "market": "bonds", "side": "buy", "y_idx_bps": 300.0, **kw}


def _meta(**kw):
    return {"name": "Газпром 1", "emitter": "Газпром капитал", "base": "KEYRATE",
            "rating": "AA", "maturity": "2029-08-11", **kw}


def test_block_spread_range():
    from datetime import date as _d
    today = _d(2026, 8, 11)
    assert core.block_matches(_trade(), _meta(), _blk(spread_min=250, spread_max=350), today)
    assert not core.block_matches(_trade(), _meta(), _blk(spread_min=350), today)
    assert not core.block_matches(_trade(), _meta(), _blk(spread_max=250), today)
    # спред не посчитан (фикс или сделка мельче порога расчёта) — при заданном
    # диапазоне это «не подходит», а не «пропустить условие»
    assert not core.block_matches(_trade(y_idx_bps=None), _meta(),
                                  _blk(spread_min=250), today)
    # без диапазона несчитанный спред ничему не мешает
    assert core.block_matches(_trade(y_idx_bps=None), _meta(), _blk(), today)


def test_block_years_range():
    from datetime import date as _d
    today = _d(2026, 8, 11)
    assert core.block_matches(_trade(), _meta(), _blk(years_min=2, years_max=4), today)
    assert not core.block_matches(_trade(), _meta(), _blk(years_max=1), today)
    # без даты погашения срок не проверить — не пускаем
    assert not core.block_matches(_trade(), _meta(maturity=None), _blk(years_max=5), today)
    assert core.block_matches(_trade(), _meta(maturity=None), _blk(), today)


def test_block_issuer_switch():
    from datetime import date as _d
    today = _d(2026, 8, 11)
    ofz = _trade(isin="RU000A0000O7", secid="SU29014RMFS6")
    ofz_meta = _meta(name="ОФЗ 29014", emitter="Минфин России")
    assert core.block_matches(ofz, ofz_meta, _blk(issuer="ofz"), today)
    assert not core.block_matches(ofz, ofz_meta, _blk(issuer="corp"), today)
    assert not core.block_matches(_trade(), _meta(), _blk(issuer="ofz"), today)
    assert core.block_matches(_trade(), _meta(), _blk(issuer="corp"), today)


def test_block_params_validation():
    with pytest.raises(core.FilterError):
        _blk(spread_min=400, spread_max=100)
    with pytest.raises(core.FilterError):
        _blk(years_min=5, years_max=2)


def test_events_feed_sorted_chronologically(monkeypatch):
    """Лента одна на два источника, а время в ней писалось двумя форматами:
    события стакана — UTC с зоной, крупные сделки (до 2026-08-20) — строкой МСК.
    Строковая сортировка мешала их в разнобой; читаем хронологически."""
    from services.portfolio_db import _connect, _lock
    from services import signals as sig

    with _lock, _connect() as c:
        c.execute("DELETE FROM signal_events WHERE user_email=?", (USER,))
        rows = [
            # (reason, fired_at) — МСК-строка 14:25 это 11:25 UTC, то есть ПОЗЖЕ 11:07
            ("block", "2026-08-20 14:25:45", "RU000A0000B2"),
            ("new", "2026-08-20T11:07:09.000000+00:00", "RU000A0000A1"),
            ("block", "2026-08-20 13:00:00", "RU000A0000C3"),
        ]
        for reason, ts, isin in rows:
            c.execute("INSERT INTO signal_events(filter_id,user_email,isin,name,side,"
                      "reason,fired_at,seen) VALUES(0,?,?,?,?,?,?,0)",
                      (USER, isin, isin, "ask", reason, ts))

    feed = sig.events_for_user(USER)
    assert [e["isin"] for e in feed] == ["RU000A0000B2",   # 11:25 UTC
                                         "RU000A0000A1",   # 11:07 UTC
                                         "RU000A0000C3"]   # 10:00 UTC
    sig.clear_events(USER)


def test_event_moment_reads_naive_as_msk():
    from services.signals import event_moment
    aware = event_moment("2026-08-20T11:25:45+00:00")
    naive = event_moment("2026-08-20 14:25:45")     # МСК без зоны — как писали блоки
    assert aware == naive


# ── Y-IDX события считается верифицированным путём, не наклоном ─────────────

def test_exact_y_idx_beats_stale_slope_anchor(monkeypatch):
    """Спред события берётся из reprice_at_price (как уровень стакана), а не из
    линейной оценки по якорю.

    Регресс: наклон считался от верха стакана, но bid/ask приходят потоком
    котировок, а лестница — батч-снимком глубины. На рассинхроне якорь уезжал, и
    у короткой бумаги (наклон до −450 bps на 1пп) событие показывало сотни bps
    мимо: РСетиМР1Р5 20.08.2026 — 378 bps на цене 100.34, где верный Y-IDX +5.
    """
    from datetime import date
    uni, metrics, depth = _market()
    isin = "RU000A0000A1"
    # якорь ПРОТУХ: цена ask уже подтянулась к лестнице, а её Y-IDX остался от
    # прежней цены (~99.5) — ровно рассинхрон «котировки vs снимок глубины»
    metrics[isin] = dict(metrics[isin], ask=100.30, yoi_ask=380.0, yoi_slope=-450.0)
    depth[isin] = {"a": [[100.30, 500], [100.35, 500]], "b": [[99.9, 100]]}

    p = core.normalize_params({"spread_min": -100, "spread_max": 500,
                               "min_money_rub": 500_000, "isins": [isin]})
    cands = core.static_candidates(p, uni, date(2026, 8, 21))

    # наклон почти не двигает число от протухшего якоря → сотни bps мимо
    slope_only = core.evaluate_candidates(p, cands, metrics, depth)[0]
    assert slope_only["val_bps"] > 300, "предусловие: наклон тащит протухший якорь"

    # точный путь: подсовываем «reprice» через прогретый контекст
    core.drop_exact_cache()
    import time as _t
    core._exact_ctx[isin] = (_t.monotonic(), {"stub": True}, {})
    monkeypatch.setattr(core, "exact_y_idx",
                        lambda i, px: 5.0 if i == isin else None)

    exact = core.evaluate_candidates(p, cands, metrics, depth, exact=True)[0]
    assert exact["val_bps"] == pytest.approx(5.0), "число должно прийти из reprice"
    assert exact["price"] == pytest.approx(slope_only["price"]), "цена набора та же"
    core.drop_exact_cache()


def test_exact_never_falls_back_to_stale_anchor():
    """Нет контекста — нет числа: молчим, а не отдаём приближение.

    Регресс: при наборе в ОДИН уровень код подставлял top_val (yoi_ask из снимка
    метрик) — тот же протухающий якорь. РСетиМР1P7 21.08.2026: 166 bps в ленте
    против верных 28. Откат на наклон/верх стакана в точном режиме запрещён."""
    from datetime import date
    uni, metrics, depth = _market()
    core.drop_exact_cache()          # ни одного тёплого контекста
    p = core.normalize_params({"spread_min": 100, "min_money_rub": 1e6})
    cands = core.static_candidates(p, uni, date(2026, 8, 21))
    got = core.evaluate_candidates(p, cands, metrics, depth, exact=True)
    assert got == [], "без точного числа бумага не проходит фильтр по спреду"

    # тот же прогон БЕЗ exact — приближение работает как раньше (превью до прогрева)
    loose = core.evaluate_candidates(p, cands, metrics, depth)
    assert [m["isin"] for m in loose] == ["RU000A0000A1"]


def test_exact_single_level_uses_reprice_not_top_val(monkeypatch):
    """Набор в один уровень: число из reprice, а не из top_val снимка метрик."""
    from datetime import date
    uni, metrics, depth = _market()
    isin = "RU000A0000B2"                      # лестница из одного уровня 99.0
    metrics[isin] = dict(metrics[isin], yoi_ask=166.0)
    p = core.normalize_params({"spread_min": 0, "min_money_rub": 40_000,
                               "isins": [isin]})
    cands = core.static_candidates(p, uni, date(2026, 8, 21))
    core.drop_exact_cache()
    import time as _t
    core._exact_ctx[isin] = (_t.monotonic(), {"stub": True}, {})
    monkeypatch.setattr(core, "exact_y_idx", lambda i, px: 28.0)
    got = core.evaluate_candidates(p, cands, metrics, depth, exact=True)
    assert got and got[0]["val_bps"] == pytest.approx(28.0)
    core.drop_exact_cache()


# ── свежесть контекста расчёта ──────────────────────────────────────────────

def test_exact_ctx_expires(monkeypatch):
    """Контекст живёт минуты, а не день. РусГид2Р01 21.08.2026: контекст,
    собранный неудачно утром, звонил 181 bps на цене, где верные 103, — и так
    до полуночи, потому что кэш был дневным."""
    import time as _t
    isin = "RU000A0000A1"
    core.drop_exact_cache()
    core._exact_ctx[isin] = (_t.monotonic() - core.EXACT_CTX_TTL_SEC - 1,
                             {"stub": True}, {})
    assert core.exact_y_idx(isin, 100.0) is None, "протухший контекст не считает"

    core._exact_ctx[isin] = (_t.monotonic(), {"stub": True}, {})
    monkeypatch.setattr(core, "reprice_at_price", lambda ctx, px: {}, raising=False)
    core.drop_exact_cache()


def test_rewarm_picks_cold_before_stale(monkeypatch):
    """Бумага без контекста греется раньше протухшей: без числа она молчит
    вовсе, а протухшее хотя бы близко."""
    import time as _t
    core.drop_exact_cache()
    core._exact_ctx["STALE"] = (_t.monotonic() - core.EXACT_CTX_TTL_SEC - 1, {}, {})
    monkeypatch.setattr(core, "EXACT_REWARM_PER_TICK", 1)

    seen = []

    async def fake_load(isin, cache):
        seen.append(isin)
        return {"ok": True}
    monkeypatch.setattr("services.bond_details.load_reprice_ctx", fake_load)
    monkeypatch.setattr(
        "services.market_data.MarketDataService.get_local_bond_cache",
        staticmethod(lambda p: {}))

    asyncio.run(core.warm_exact_ctx(["STALE", "COLD"]))
    assert seen == ["COLD"], "холодная бумага вперёд протухшей"
    core.drop_exact_cache()


def test_memo_dies_with_its_context(monkeypatch):
    """Пересборка контекста обнуляет и запомненные числа: иначе протухшее
    значение пережило бы собственный контекст."""
    import time as _t
    # синк кривых проверяется отдельно (test_curve_swap_invalidates_memo) и
    # здесь только мешает: он сам чистит memo, если в кэше лежит другая кривая
    monkeypatch.setattr(core, "_sync_ctx_curves", lambda ctx, memo: None)
    core.drop_exact_cache()
    core._exact_ctx["X"] = (_t.monotonic(), {"stub": True}, {100.0: 999.0})
    assert core.exact_y_idx("X", 100.0) == 999.0          # взято из memo
    core._exact_ctx["X"] = (_t.monotonic(), {"stub": True}, {})
    assert core.exact_y_idx("X", 100.0) != 999.0          # memo уехал с контекстом
    core.drop_exact_cache()


def test_curve_swap_invalidates_memo(monkeypatch):
    """Кривая — живой объект рынка: пересобралась (стейл-котировки Cbonds
    перепроверяются раз в 15 минут) — запомненные числа больше не годятся.

    Регресс РусГид2Р01 21.08.2026: контекст держал ссылку на утреннюю кривую,
    индекс 13,98% против текущих 14,76% → 181 bps вместо 103."""
    import time as _t
    from services.market_data import market_cache

    class _Curve:
        def __init__(self, tag):
            self.tag = tag

    class _Ref:
        base = "KEYRATE"

    old_curve, new_curve = _Curve("утро"), _Curve("день")
    monkeypatch.setitem(market_cache, "keyrate_curve", old_curve)
    monkeypatch.setitem(market_cache, "ruonia_curve", None)

    ctx = {"ref_obj": _Ref(), "curve": old_curve}
    memo = {100.23: 181.0}
    core._exact_ctx["Y"] = (_t.monotonic(), ctx, memo)

    monkeypatch.setattr(core, "_ctx_fresh", lambda rec: True)
    assert core.exact_y_idx("Y", 100.23) == 181.0      # кривая та же — memo жив

    market_cache["keyrate_curve"] = new_curve
    seen = {}

    def fake_reprice(c, px):
        seen["curve"] = c["curve"]
        return {"yield_over_index_bps": 103.0}
    monkeypatch.setattr("services.bond_details.reprice_at_price", fake_reprice)
    monkeypatch.setattr("services.valuation.pick_horizon", lambda m, h: m)

    assert core.exact_y_idx("Y", 100.23) == 103.0, "memo сброшен, счёт заново"
    assert seen["curve"] is new_curve, "считали на новой кривой"
    core.drop_exact_cache()


# ── адресаты доставки: канал на фильтр ─────────────────────────────────────

@pytest.fixture()
def targets(clean_db):
    from services import tg_targets
    t1 = tg_targets.add(USER, -1001, "Р5", "channel")
    t2 = tg_targets.add("other@x.ru", -1002, "Чужой", "channel")
    yield t1, t2
    for t in (t1, t2):
        tg_targets.remove(t["user_email"], t["id"])


def test_filter_keeps_its_channel(targets):
    """Фильтр «Р5» шлёт в канал «Р5»; без адресата — в личку, как раньше."""
    t1, _ = targets
    f = signals.create(USER, "Р5", {"spread_min": 100}, tg_target_id=t1["id"])
    assert f["tg_target_id"] == t1["id"]
    assert signals.create(USER, "Ф5", {"spread_min": 100})["tg_target_id"] is None

    upd = signals.update(USER, f["id"], tg_target_id=None)
    assert upd["tg_target_id"] is None, "null убирает канал"
    upd = signals.update(USER, f["id"], name="Р5 новый")
    assert upd["name"] == "Р5 новый", "поле без tg_target_id не трогает адресата"


def test_foreign_channel_is_rejected(targets):
    """Чужой канал по подобранному id не привязать."""
    _, foreign = targets
    with pytest.raises(signals.FilterError):
        signals.create(USER, "Р5", {"spread_min": 100},
                       tg_target_id=foreign["id"])


def test_removed_channel_falls_back_to_private(targets):
    """Канал отвязали — фильтр не молчит, а возвращается к доставке в личку."""
    from services import tg_targets
    t1, _ = targets
    f = signals.create(USER, "Р5", {"spread_min": 100}, tg_target_id=t1["id"])
    tg_targets.remove(USER, t1["id"])
    assert tg_targets.chat_id_for(f["tg_target_id"], USER) is None


# ── объём в сообщении: накопленный объём до границы набора ────────────────

def test_money_upto_price_is_cumulative_depth():
    """Сколько денег стоит по цене НЕ ХУЖЕ заданной.

    Регресс Газпн3P13R 24.08: в шапке было 20,7м (вся сторона книги) при 3,8м,
    доступных по 99,86 — цене, по которой фильтр и сработал."""
    ask = [(99.86, 3800), (99.87, 2200), (99.90, 2000), (99.91, 2000)]
    assert core.money_upto(ask, 99.86, "ask", 1000.0) == pytest.approx(3_794_680)
    assert core.money_upto(ask, 99.87, "ask", 1000.0) == pytest.approx(
        3_794_680 + 2_197_140, rel=1e-6)
    assert core.money_upto(ask, 99.91, "ask", 1000.0) == pytest.approx(
        9_985_800, rel=1e-3)


def test_money_upto_price_bid_side_is_mirrored():
    """У бида «не хуже» — это ДОРОЖЕ-или-равно."""
    bid = [(99.82, 2202), (99.80, 100), (99.78, 80)]
    assert core.money_upto(bid, 99.82, "bid", 1000.0) == pytest.approx(
        2_202 * 998.2, rel=1e-6)
    assert core.money_upto(bid, 99.80, "bid", 1000.0) == pytest.approx(
        2_202 * 998.2 + 100 * 998.0, rel=1e-6)


def test_money_upto_price_empty_side():
    assert core.money_upto([], 99.9, "ask", 1000.0) is None
    assert core.money_upto([(99.9, 10)], None, "ask", 1000.0) is None


def test_depth_is_measured_at_last_taken_level_not_vwap():
    """Граница накопления — цена ПОСЛЕДНЕГО взятого уровня, а не средневзвес.

    Регресс Газпн3P14R 24.08: набор занял 7 уровней, средневзвес 101,18 оказался
    лучше трёх худших из них, и накопленный объём по нему вышел 250,5к — меньше
    самого набранного миллиона."""
    ask = [(101.08, 204), (101.09, 184), (101.13, 10), (101.14, 31),
           (101.20, 300), (101.25, 200), (101.30, 500)]
    v = core.vwap_for(ask, 1_400_000, face=1000.0)
    assert v["levels"] == 7 and v["partial"] is False
    assert v["last_px"] == 101.30, "граница — худший взятый уровень"
    assert v["px"] < v["last_px"], "средневзвес всегда лучше границы"

    by_vwap = core.money_upto(ask, v["px"], "ask", 1000.0)
    by_edge = core.money_upto(ask, v["last_px"], "ask", 1000.0)
    assert by_vwap < v["money"], "предусловие: по средневзвесу выходит меньше набора"
    assert by_edge >= v["money"], "по границе — не меньше того, что набрали"


# ── повтор только по спреду ────────────────────────────────────────────────

def _state_row(fid, isin, val_bps, money_ok):
    """Кладёт прошлое состояние бумаги, как его пишет тик скринера."""
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with _lock, _connect() as c:
        c.execute(
            "INSERT INTO signal_state(filter_id,isin,val_bps,price,money_rub,"
            "money_ok_rub,last_seen_at,last_event_at,last_reason,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fid, isin, val_bps, 100.0, 1e6, money_ok, old, old, "new", old))


def test_money_trigger_can_be_switched_off():
    """repeat_on_money=False: объём вырос вдвое — молчим, спред не двигался."""
    f = signals.create(USER, "только спред",
                       {"spread_min": 100, "repeat_on_money": False})
    assert f["params"]["repeat_on_money"] is False
    _state_row(f["id"], "RU000A0000A1", 150.0, 1_000_000)
    m = [{"isin": "RU000A0000A1", "name": "Б", "val_bps": 150.0, "price": 100.0,
          "money_rub": 2e6, "money_ok_rub": 2_000_000}]
    assert signals.detect_events(f["id"], USER, "ask", 10.0, m, None,
                                 repeat_on_money=False) == []


def test_spread_trigger_still_fires_when_money_off():
    """Тот же фильтр: спред ушёл — событие есть."""
    f = signals.create(USER, "только спред 2",
                       {"spread_min": 100, "repeat_on_money": False})
    _state_row(f["id"], "RU000A0000A2", 150.0, 1_000_000)
    m = [{"isin": "RU000A0000A2", "name": "Б", "val_bps": 190.0, "price": 100.0,
          "money_rub": 1e6, "money_ok_rub": 1_000_000}]
    ev = signals.detect_events(f["id"], USER, "ask", 10.0, m, None,
                               repeat_on_money=False)
    assert [e["reason"] for e in ev] == ["spread"]


def test_money_trigger_on_by_default():
    """Умолчание не меняется: старые фильтры звонят и на объём."""
    f = signals.create(USER, "как раньше", {"spread_min": 100})
    assert f["params"]["repeat_on_money"] is True
    _state_row(f["id"], "RU000A0000A3", 150.0, 1_000_000)
    m = [{"isin": "RU000A0000A3", "name": "Б", "val_bps": 150.0, "price": 100.0,
          "money_rub": 2e6, "money_ok_rub": 2_000_000}]
    ev = signals.detect_events(f["id"], USER, "ask", 10.0, m, None)
    assert [e["reason"] for e in ev] == ["money"]


# --- люфт порога объёма (screener_core.money_floor) ---

def test_money_floor_is_ten_percent():
    """Порог объёма — про ПОРЯДОК, а не про границу до рубля: 48 млн под
    фильтром «от 50» — ровно та сделка, ради которой фильтр заведён."""
    assert core.money_floor(50e6) == 45e6
    assert core.money_floor(None) is None
    assert core.money_floor(0) is None


def test_block_value_has_tolerance():
    """Сделка чуть мельче порога попадает и в алерты, и в таблицу; заметно
    мельче — по-прежнему нет."""
    from datetime import date as _d
    today = _d(2026, 8, 11)
    assert core.block_matches(_trade(value=4_600_000), _meta(), _blk(), today)
    assert not core.block_matches(_trade(value=4_400_000), _meta(), _blk(), today)


def test_single_mode_order_has_tolerance():
    """Крупная заявка чуть мельче порога — тоже крупная заявка: 6,0 млн под
    фильтром «от 6,5» проходит, под «от 7» уже нет."""
    uni, metrics, _small, big = _big_small()   # одна заявка на 6,003 млн
    p = core.normalize_params({"ratings": ["AAA"], "min_money_rub": 6.5e6,
                               "money_mode": "single"})
    m = core.evaluate(p, uni, metrics, big)[0]
    assert m["levels"] == 1 and m["money_rub"] == pytest.approx(6_003_000)

    p2 = core.normalize_params({"ratings": ["AAA"], "min_money_rub": 7e6,
                                "money_mode": "single"})
    assert core.evaluate(p2, uni, metrics, big) == []


def test_tg_book_keeps_exchange_order():
    """Лестница — как в терминале: офферы сверху вниз до лучшего, под чертой
    биды от лучшего вниз, цена по столбцу монотонно падает.

    Пробовали ставить сторону сигнала первой (ради двух строк, видимых под
    свёрнутой цитатой) — перевёрнутый стакан читается неверно целиком, а цена
    и спред события и так стоят в шапке сообщения, НАД цитатой."""
    from services import tg_notify

    ev = {
        "price": 100.10,
        "book": {
            # как отдаёт book_snapshot: офферы худшим-первым, биды лучшим-первым
            "asks": [{"price": 100.43, "qty": 334, "y_idx": 166},
                     {"price": 100.20, "qty": 67892, "y_idx": 181},
                     {"price": 100.10, "qty": 50000, "y_idx": 188}],
            "bids": [{"price": 100.00, "qty": 1000, "y_idx": 195}],
        },
    }
    lines = [ln for ln in tg_notify._book_pre(ev, "ask").split("\n")
             if "ЦЕНА" not in ln]
    order = [ln for ln in lines if "100," in ln]
    idx = lambda p: next(i for i, l in enumerate(order) if p in l)
    assert idx("100,43") < idx("100,20") < idx("100,10") < idx("100,00"), \
        f"порядок не биржевой: {order}"
    assert "←" in order[idx("100,10")], "уровень события не помечен стрелкой"
    assert "←" not in order[idx("100,20")], "чужой уровень помечен"


def test_ctx_takes_accrued_from_board_when_snapshot_misses(monkeypatch):
    """Персональный снимок MOEX без НКД → берём его из борд-снимка.

    Прод 27.08.2026: у контекста НКД оказался None, расчёт начислил своё, и
    точный путь разошёлся с таблицей (РЕСОЛизБО5 368 против 382; ВЭБ2Р-53 166
    против 188 — там же уехала вся лестница стакана в телеграме). Начисление в
    десятые доли рубля стоит десятков б.п. спреда, поэтому НКД обязан
    приезжать из того же источника, которым считает витрина."""
    import asyncio
    from services import bond_details as bd

    isin = "RU000TESTACC1"

    async def _snap(ids):                    # персональный снимок без НКД
        return {isin: {"prev": 99.0}}

    async def _board():                      # борд-снимок — с НКД
        return {isin: {"prev": 99.0, "accrued": 7.77, "accrued_date": "2026-08-28"}}

    async def _sched(ids):
        return {}

    async def _full(i):
        return {"coupons": [], "amorts": [], "offers": []}

    class _Curve:
        rate_convention = "daily_comp"

    async def _curves():
        from datetime import date
        return _Curve(), _Curve(), date(2026, 8, 27), date(2026, 8, 26)

    async def _z():
        return None, None, None

    monkeypatch.setattr(bd.MarketDataService, "fetch_moex_snapshot", staticmethod(_snap))
    monkeypatch.setattr(bd.MarketDataService, "fetch_board_snapshot", staticmethod(_board))
    monkeypatch.setattr(bd.MarketDataService, "fetch_coupon_schedules", staticmethod(_sched))
    monkeypatch.setattr(bd.MarketDataService, "fetch_bond_schedule_full", staticmethod(_full))
    monkeypatch.setattr(bd.MarketDataService, "get_curves", staticmethod(_curves))
    monkeypatch.setattr(bd.MarketDataService, "get_zspread_ctx", staticmethod(_z))
    monkeypatch.setattr(bd, "reconcile_face", lambda *a, **k: None)
    monkeypatch.setattr(bd, "amort_remaining_face", lambda *a, **k: None)

    class _Ref:
        base = "RUONIA"
        face_value = 1000.0
        accrued_rub = None

    _Ref.isin = isin

    monkeypatch.setattr(bd, "create_bond_ref_data", lambda data, i: _Ref())
    # непустая строка кэша: пустой dict falsy и увёл бы во внешний путь MOEX
    ctx = asyncio.run(bd.load_reprice_ctx(isin, {isin: {"isin": isin}}))
    assert ctx["accrued_live"] == 7.77, "НКД не добрался из борд-снимка"
    assert str(ctx["accrued_date"]) == "2026-08-28", "дата НКД должна ехать из того же источника"
    assert ctx["accrued_missing"] is False


def test_exact_y_idx_silent_without_accrued(monkeypatch):
    """Без НКД точного числа не бывает — молчим, а не показываем сдвинутое."""
    import time
    from services import screener_core as core

    isin = "RU000TESTACC2"
    core._exact_ctx[isin] = (time.monotonic(), {"accrued_missing": True,
                                                "ref_obj": None}, {})
    try:
        assert core.exact_y_idx(isin, 100.0) is None
    finally:
        core._exact_ctx.pop(isin, None)


def test_tg_book_marks_levels_eaten_by_the_set():
    """Спред шапки считается по средневзвесу набора, и такой цены в книге нет.
    Значит помечены должны быть ВСЕ съеденные уровни — иначе читатель видит
    «в шапке 162, а в стакане 172» и перестаёт верить цифрам."""
    from services import tg_notify

    ev = {
        "price": 99.9993, "levels": 2,      # набор лёг в два верхних уровня
        "book": {
            "asks": [{"price": 100.10, "qty": 50, "y_idx": 154},
                     {"price": 99.98, "qty": 122, "y_idx": 164},
                     {"price": 99.89, "qty": 89, "y_idx": 172}],
            "bids": [{"price": 99.50, "qty": 10, "y_idx": 205}],
        },
    }
    rows = [ln for ln in tg_notify._book_pre(ev, "ask").split("\n") if "ЦЕНА" not in ln]
    ask_rows = [ln for ln in rows if "99,89" in ln or "99,98" in ln or "100,10" in ln]
    # порядок биржевой (худший оффер сверху), набор съел два ЛУЧШИХ уровня
    assert "←" in ask_rows[1] and "←" in ask_rows[2], "взятые уровни не помечены"
    assert "←" not in ask_rows[0], "нетронутый уровень помечен как взятый"


def test_money_in_spread_uses_methodology_not_slope(monkeypatch):
    """Объём «по нашим условиям» отбирает уровни по ТОЧНОМУ спреду уровня.

    Наклон убран 27.08.2026: он мерил уровни числами, уехавшими вслед за
    якорем, и метрика повторного сигнала срабатывала не на том объёме. Здесь
    якорь заведомо протухший — если бы отбор шёл наклоном, в диапазон попали бы
    другие уровни (или ни одного)."""
    ladder = [[100.10, 1000], [100.20, 2000], [100.30, 3000]]
    row = {"ask": 99.00, "yoi_ask": 900.0, "yoi_slope": -450.0}   # якорь уехал
    exact = {100.10: 180.0, 100.20: 160.0, 100.30: 140.0}
    monkeypatch.setattr(core, "exact_y_idx_map",
                        lambda isin, pxs: {round(float(p), core._EXACT_PX_DIGITS):
                                           exact.get(round(float(p), 2))
                                           for p in pxs if p is not None})

    # диапазон 150–200 бп накрывает два верхних уровня
    got = core.money_in_spread(ladder, row, "ask", 150, 200, 1000.0, 0.0, "RU000TESTMIS1")
    want = sum(core.level_money(px, qty, 1000.0, 0.0) for px, qty in ladder[:2])
    assert got == pytest.approx(want), "отобраны не те уровни"

    # без контекста числа нет — метрика молчит, а не считает наклоном
    monkeypatch.setattr(core, "exact_y_idx_map", lambda isin, pxs: {})
    assert core.money_in_spread(ladder, row, "ask", 150, 200, 1000.0, 0.0, "X") is None


def test_years_filter_uses_pricing_horizon():
    """Окно срока — до ГОРИЗОНТА ПРАЙСИНГА, как в мониторе (App.jsx hzDate).

    Регресс: скринер/портфель/бот мерили срок только до погашения, и бумага с
    путом через полгода (спред у неё посчитан к этому путу) не попадала в
    фильтр «до 2 лет», хотя в таблице под ним стояла."""
    from datetime import date
    uni, metrics, depth = _market()
    today = date(2026, 8, 21)
    isin = "RU000A0000A1"                       # погашение 2029-08-11
    metrics[isin] = dict(metrics[isin], horizon="put", offer_date="2027-02-11")
    p = core.normalize_params({"years_max": 2, "spread_min": -1000, "spread_max": 1000})

    cands = core.static_candidates(p, uni, today, metrics)
    assert isin in [c["isin"] for c in cands], "бумага с путом через 0.5г попадает в окно 2г"

    # горизонт снялся (цена ушла выше цены выкупа) → срок снова до погашения,
    # и свежая проверка в рыночной стадии убирает бумагу даже из кеша кандидатов
    metrics[isin] = dict(metrics[isin], horizon="maturity", offer_date=None)
    got = core.evaluate_candidates(p, cands, metrics, depth)
    assert isin not in [m["isin"] for m in got]


def test_book_columns_aligned_without_monospace():
    """Лестница набрана ОБЫЧНЫМ шрифтом, а колонки всё равно ровные.

    Держится это на U+2007 (figure space) — пробеле шириной ровно в цифру:
    обычный пробел ýже, и колонки разъезжались тем сильнее, чем разнее длина
    чисел. Проверяем инвариант: в каждой строке колонки одной ширины, и ни
    одного моноширинного тега.
    """
    from services import tg_notify
    from services.tg_notify import _FIG, _GAP

    ev = {
        "price": 100.10, "levels": 1,
        "book": {
            "asks": [{"price": 100.43, "qty": 334, "y_idx": 166},
                     {"price": 100.20, "qty": 67892, "y_idx": 181},
                     {"price": 100.10, "qty": 120000, "y_idx": 188}],
            "bids": [{"price": 99.85, "qty": 3400, "y_idx": 205}],
        },
    }
    out = tg_notify._book_pre(ev, "ask")
    assert "<code>" not in out, "моноширинный шрифт вернулся"
    inner = out.replace("<blockquote expandable>", "").replace("</blockquote>", "")
    rows = [ln for ln in inner.split("\n") if "," in ln]
    assert len(rows) == 4, f"ожидались четыре уровня, вышло {len(rows)}: {rows}"
    # каждая строка — фиксированные колонки 6+7+4 и два промежутка по три:
    # ширина в «цифрах» одна на всех, потому и стоят ровно
    widths = {len(ln.replace(" ←", "")) for ln in rows}
    assert widths == {6 + len(_GAP) + 7 + len(_GAP) + 4}, f"строки разной ширины: {widths}"
    assert f"67{_FIG}892" in rows[1], "разряды количества разделены не figure space"
