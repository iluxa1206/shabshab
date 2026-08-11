"""Вкладка СИГНАЛЫ: ядро скринера, хранение фильтров, лента, анти-спам."""
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
        c.execute("DELETE FROM signal_hits WHERE user_email=?", (USER,))


def _market():
    uni = [
        {"isin": "RU000A0000A1", "name": "Газпром 1", "rating": "AA",
         "emitter_name": "Газпром капитал", "maturity_date": "2029-08-11"},
        {"isin": "RU000A0000B2", "name": "Мелкий БО", "rating": "BBB",
         "emitter_name": "Мелкая контора", "maturity_date": "2027-02-10"},
        {"isin": "RU000A0000C3", "name": "ВЭБ 3", "rating": "AAA",
         "emitter_name": "ВЭБ.РФ", "maturity_date": "2031-08-11"},
    ]
    metrics = {
        "RU000A0000A1": {"yoi_ask": 280.0, "yoi_bid": 300.0, "ask": 100.2, "bid": 99.9, "face_px": 1000.0},
        "RU000A0000B2": {"yoi_ask": 400.0, "yoi_bid": 420.0, "ask": 99.0, "bid": 98.5, "face_px": 1000.0},
        "RU000A0000C3": {"yoi_ask": 180.0, "yoi_bid": 195.0, "ask": 100.0, "bid": 99.8, "face_px": 1000.0},
    }
    depth = {"RU000A0000A1": {"a": [[100.2, 3000]], "b": [[99.9, 100]]},
             "RU000A0000B2": {"a": [[99.0, 50]], "b": []}}
    return uni, metrics, depth


def test_core_shared_with_tg():
    from services import tg_screener
    assert tg_screener.normalize_params is core.normalize_params
    assert tg_screener.evaluate is core.evaluate
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
    f = signals.create(USER, "мой сигнал", {"spread_min": 250}, cooldown_min=15)
    assert f["enabled"] and f["sound"] and f["desktop"]
    assert f["params"]["side"] == "ask"

    assert len(signals.list_for_user(USER)) == 1
    assert signals.list_for_user("other@example.com") == []
    assert signals.update("other@example.com", f["id"], enabled=False) is None
    assert signals.delete("other@example.com", f["id"]) is False

    upd = signals.update(USER, f["id"], enabled=False, sound=False, cooldown_min=60)
    assert upd["enabled"] is False and upd["sound"] is False and upd["cooldown_min"] == 60
    assert signals.delete(USER, f["id"]) is True


def test_create_validates():
    with pytest.raises(signals.FilterError):
        signals.create(USER, "", {"spread_min": 250})
    with pytest.raises(signals.FilterError):
        signals.create(USER, "x", {})
    with pytest.raises(signals.FilterError):
        signals.create(USER, "x", {"spread_min": 250}, cooldown_min=99999)


def test_hits_feed_and_cooldown():
    f = signals.create(USER, "лента", {"spread_min": 250}, cooldown_min=60)
    matches = [{"isin": "RU000A0000A1", "name": "Газпром 1", "val_bps": 280.0,
                "price": 100.2, "money_rub": 3.0e6}]
    fresh = signals.fresh_matches(f["id"], USER, 60, "ask", matches)
    assert len(fresh) == 1 and fresh[0]["fired_at"]
    assert signals.fresh_matches(f["id"], USER, 60, "ask", matches) == []

    feed = signals.hits_for_user(USER)
    assert len(feed) == 1
    assert feed[0]["isin"] == "RU000A0000A1" and feed[0]["filter_name"] == "лента"
    assert feed[0]["seen"] == 0 and feed[0]["val_bps"] == 280.0

    assert signals.mark_seen(USER) == 1
    assert signals.hits_for_user(USER)[0]["seen"] == 1
    assert signals.clear_hits(USER) == 1
    assert signals.hits_for_user(USER) == []


def test_delete_filter_drops_its_hits():
    f = signals.create(USER, "удалить", {"spread_min": 250})
    signals.fresh_matches(f["id"], USER, 60, "ask",
                          [{"isin": "RU000A0000A1", "name": "X", "val_bps": 280.0,
                            "price": 100.0, "money_rub": None}])
    assert len(signals.hits_for_user(USER)) == 1
    signals.delete(USER, f["id"])
    assert signals.hits_for_user(USER) == []


def test_run_cycle_pushes_to_owner(monkeypatch):
    import asyncio
    uni, metrics, depth = _market()

    async def fake_snapshot():
        return uni, metrics, depth
    monkeypatch.setattr(signals, "market_snapshot", fake_snapshot)

    sent = []
    from api.routes import ws as wsmod

    async def fake_broadcast(email, payload):
        sent.append((email, payload))
    monkeypatch.setattr(wsmod.manager, "broadcast_signal", fake_broadcast)

    f = signals.create(USER, "цикл", {"spread_min": 250, "side": "ask"}, cooldown_min=60)
    assert asyncio.run(signals.run_cycle()) >= 1
    mine = [s for s in sent if s[0] == USER]
    assert len(mine) == 1
    payload = mine[0][1]
    assert payload["filter_name"] == "цикл" and payload["side"] == "ask"
    assert {m["isin"] for m in payload["matches"]} == {"RU000A0000A1", "RU000A0000B2"}

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
