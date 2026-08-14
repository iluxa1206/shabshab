"""Отмеченные сделки ленты («красный флажок»).

Хранится СНИМОК строки, а не ссылка на trade_tick: тиковый архив подчищается
ретеншеном, и отметка на мелкой сделке через месяц указывала бы в пустоту.
"""
import importlib

import pytest


@pytest.fixture
def flags(tmp_path, monkeypatch):
    """Слой флагов на изолированной БД."""
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "portfolio.db"))
    from services import portfolio_db
    importlib.reload(portfolio_db)
    portfolio_db.init_db()
    from services import trade_flags
    importlib.reload(trade_flags)
    yield trade_flags


TRADE = {"trade_id": 5001, "isin": "RU000A10B4T4", "ts": "2026-08-14 11:20:31",
         "price": 100.4, "qty": 50, "value": 5_020_000, "side": "buy",
         "board": "TQCB", "market": "bonds", "cur": "SUR",
         "y_idx_bps": 651, "yld": 22.1}


def test_add_and_list(flags):
    flags.add("a@x", TRADE)
    rows = flags.listing("a@x")
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_id"] == 5001 and r["isin"] == "RU000A10B4T4"
    # снимок целиком: список флагов самодостаточен и переживает ретеншен тиков
    assert r["value"] == 5_020_000 and r["side"] == "buy" and r["y_idx_bps"] == 651
    assert r["flagged"] is True and r["negotiated"] is False


def test_idempotent_update(flags):
    flags.add("a@x", TRADE)
    flags.add("a@x", {**TRADE, "value": 6_000_000}, note="перекрыли")
    rows = flags.listing("a@x")
    assert len(rows) == 1                      # дубля нет
    assert rows[0]["value"] == 6_000_000       # снимок обновился
    assert rows[0]["note"] == "перекрыли"


def test_per_user_isolation(flags):
    flags.add("a@x", TRADE)
    assert flags.ids("a@x") == {5001}
    assert flags.ids("b@x") == set()
    assert flags.listing("b@x") == []
    # чужой флаг не снимается
    assert flags.remove("b@x", 5001) is False
    assert flags.ids("a@x") == {5001}


def test_remove(flags):
    flags.add("a@x", TRADE)
    assert flags.remove("a@x", 5001) is True
    assert flags.remove("a@x", 5001) is False   # повторное — no-op, не ошибка
    assert flags.ids("a@x") == set()


def test_ndm_flag_marks_negotiated(flags):
    flags.add("a@x", {**TRADE, "trade_id": 5002, "market": "ndm"})
    r = [x for x in flags.listing("a@x") if x["trade_id"] == 5002][0]
    assert r["negotiated"] is True


@pytest.mark.parametrize("bad", [
    {**TRADE, "trade_id": None},
    {**TRADE, "isin": ""},
    {**TRADE, "ts": ""},
])
def test_required_fields(flags, bad):
    with pytest.raises(ValueError):
        flags.add("a@x", bad)


def test_order_newest_first(flags):
    flags.add("a@x", {**TRADE, "trade_id": 1, "ts": "2026-08-10 10:00:00"})
    flags.add("a@x", {**TRADE, "trade_id": 2, "ts": "2026-08-14 10:00:00"})
    assert [r["trade_id"] for r in flags.listing("a@x")] == [2, 1]
