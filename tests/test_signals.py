"""Вкладка СИГНАЛЫ: ядро скринера, хранение фильтров, лента, анти-спам."""
import asyncio

import pytest

from services import screener_core as core
from services import signals
from services.portfolio_db import _connect, _lock, init_db

USER = "signals-test@example.com"


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    yield
    with _lock, _connect() as c:
        c.execute("DELETE FROM signal_filters WHERE user_email=?", (USER,))
        c.execute("DELETE FROM signal_events WHERE user_email=?", (USER,))
        c.execute("DELETE FROM signal_state WHERE filter_id NOT IN "
                  "(SELECT id FROM signal_filters)")


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


def test_events_money_change_and_leaving_set():
    uni, metrics, depth = _market()
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


# ── объём в сообщении: накопленный объём по цене сигнала ──────────────────

def test_money_upto_price_is_cumulative_depth():
    """Сколько денег стоит по цене НЕ ХУЖЕ цены сигнала.

    Регресс Газпн3P13R 24.08: в шапке было 20,7м (вся сторона книги) при 3,8м,
    доступных по 99,86 — цене, по которой фильтр и сработал."""
    ask = [(99.86, 3800), (99.87, 2200), (99.90, 2000), (99.91, 2000)]
    # по лучшей цене доступен только свой уровень
    assert core.money_upto(ask, 99.86, "ask", 1000.0) == pytest.approx(3_794_680)
    # цена глубже — накапливаем всё, что дешевле-или-равно
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
