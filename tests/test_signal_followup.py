"""Судьба заявки: что стало с уровнем через четверть часа после сигнала.

Классификация чистая, поэтому проверяется без стакана и без сети; отдельно —
что проверка ставится только на «заявку» и что ответ уходит реплаем.
"""
import asyncio
import importlib

import pytest


@pytest.fixture()
def fu(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.signal_followup as m
    importlib.reload(m)
    return m


# ── пороги исходов ─────────────────────────────────────────────────────────

def test_kept_when_depth_survives(fu):
    """Стакан дышит мелочью: −2 % это не «сняли»."""
    assert fu.classify(5_900_000, 5_800_000, 0) == "kept"


def test_partial_when_half_left(fu):
    assert fu.classify(5_900_000, 3_000_000, 1_000_000) == "partial"


def test_taken_needs_trades(fu):
    """Объём ушёл И сделки были — забрали."""
    assert fu.classify(5_900_000, 200_000, 5_000_000) == "taken"


def test_pulled_when_gone_without_trades(fu):
    """Тот же уход объёма, но сделок почти нет — заявку сняли.

    Порог долевой, а не «хоть одна сделка»: снятие заявки на 25 млн и
    случайный принт на 100к иначе выглядели бы одинаково."""
    assert fu.classify(5_900_000, 200_000, 100_000) == "pulled"


def test_unknown_depth_gives_no_outcome(fu):
    """Исходного объёма нет — исхода нет.

    Регресс РостелP21R 24.08: набор занял 11 уровней, уровня с ценой
    средневзвеса в книге не было, объём вышел None — и молчание выдавалось за
    «стоит»."""
    assert fu.classify(None, 1_000_000, 0) is None
    assert fu.classify(0, None, 0) is None


# ── постановка и отправка ──────────────────────────────────────────────────

_MATCH = {"isin": "RU000A1", "name": "Тест", "price": 99.75, "val_bps": 173.0,
          "level_money_rub": 24_900_000, "reason": "new",
          "book": {"asks": [{"price": 99.75, "qty": 24950, "y_idx": 173}],
                   "bids": []}}


def test_schedule_takes_level_qty_from_snapshot(fu):
    fu.schedule(42, 7, _MATCH, "ask")
    rows = fu.due(limit=10)
    assert rows == [] , "срок ещё не наступил"
    with fu._connect() as c:
        r = dict(c.execute("SELECT * FROM signal_followup").fetchone())
    assert r["chat_id"] == 42 and r["message_id"] == 7
    assert r["qty"] == 24950, "количество бумаг взято из снимка стакана"
    assert r["money"] == 24_900_000, "накопленный объём — то, с чем сравниваем"
    assert r["price"] == 99.75 and r["val_bps"] == 173.0


def test_only_new_events_are_armed(monkeypatch):
    """Follow-up ставится на «заявку», но не на повторы: иначе у одной бумаги
    накопится цепочка ответов."""
    from services import tg_notify
    armed = []
    monkeypatch.setattr("services.signal_followup.schedule",
                        lambda chat, mid, m, side: armed.append(m["reason"]))
    for reason in ("new", "spread", "money"):
        tg_notify._arm_followup(1, {"message_id": 5},
                                {"kind": "book", "side": "ask",
                                 "matches": [dict(_MATCH, reason=reason)]})
    tg_notify._arm_followup(1, {"message_id": 5},
                            {"kind": "block", "matches": [dict(_MATCH)]})
    assert armed == ["new"]


def test_render_shows_outcome_and_spread_move(fu):
    row = {"price": 99.75, "qty": 24950, "money": 24_900_000,
           "val_bps": 173.0, "side": "ask"}
    taken = fu.render(row, "taken", 100_000, 99.80, 18000, 18_400_000, 165.0)
    assert "забрали" in taken and "18,4м ₽" in taken and "RS 173 → 165" in taken
    pulled = fu.render(row, "pulled", 0, 99.80, 0, 0, 171.0)
    assert "сняли" in pulled and "без сделок" in pulled
    kept = fu.render(row, "kept", 24_500_000, 99.75, 0, 0, 173.0)
    assert "стоит" in kept and "24,5м ₽" in kept


def test_kept_is_sent_silently(fu, monkeypatch):
    """«Стоит» приходит ответом, но без звука — решение юзера 24.08."""
    fu.schedule(42, 7, _MATCH, "ask")
    with fu._lock, fu._connect() as c:
        c.execute("UPDATE signal_followup SET due_at='2000-01-01T00:00:00+00:00'")

    monkeypatch.setattr(fu, "level_now", lambda i, s, p: (24_900_000, 99.75))
    monkeypatch.setattr(fu, "traded_since", lambda i, s, p, f: (0, 0))
    monkeypatch.setattr("services.screener_core.warm_exact_ctx",
                        lambda isins: asyncio.sleep(0))
    monkeypatch.setattr("services.screener_core.exact_y_idx", lambda i, p: 173.0)

    calls = []

    async def fake_send(chat_id, text, **kw):
        calls.append(kw)
        return {"message_id": 8}
    monkeypatch.setattr("services.telegram.send_message", fake_send)

    assert asyncio.run(fu.run_due()) == 1
    assert calls[0]["disable_notification"] is True
    assert calls[0]["reply_to"] == 7, "ответ на исходный сигнал"
    assert fu.due() == [], "проверка закрыта, повтора не будет"


def test_no_outcome_means_no_message(fu, monkeypatch):
    """Не знаем исходного объёма — молчим, а не сообщаем выдуманное «стоит»."""
    m = dict(_MATCH, level_money_rub=None, book={"asks": [], "bids": []})
    fu.schedule(42, 7, m, "ask")
    with fu._lock, fu._connect() as c:
        c.execute("UPDATE signal_followup SET due_at='2000-01-01T00:00:00+00:00'")

    monkeypatch.setattr(fu, "level_now", lambda i, s, p: (1_000_000, 99.75))
    monkeypatch.setattr(fu, "traded_since", lambda i, s, p, f: (0, 0))
    monkeypatch.setattr("services.screener_core.warm_exact_ctx",
                        lambda isins: asyncio.sleep(0))
    monkeypatch.setattr("services.screener_core.exact_y_idx", lambda i, p: 173.0)
    sent = []
    monkeypatch.setattr("services.telegram.send_message",
                        lambda *a, **k: sent.append(1))

    assert asyncio.run(fu.run_due()) == 0
    assert sent == []
    with fu._connect() as c:
        assert c.execute("SELECT outcome FROM signal_followup").fetchone()[0] == "unknown"
