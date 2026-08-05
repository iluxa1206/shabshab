"""Общерыночная лента сделок (вкладка СДЕЛКИ).

Ключевое отличие от ленты выпуска: фильтр идёт по времени БЕЗ isin, а итоги
окна считаются по ВСЕМ подходящим сделкам, а не по срезанным лимитом строкам —
иначе «оборот» врал бы ровно на хвост, который не влез на страницу.
"""
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def ta(tmp_path, monkeypatch):
    """services.trades_archive на пустой временной БД."""
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.trades_archive as mod
    importlib.reload(mod)
    yield mod
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(mod)


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _add(ta, isin, tid, days_ago, value, hour=12, side="buy"):
    with ta._lock, ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (isin, tid, f"{_iso(days_ago)} {hour:02d}:00:00", 100.0, 1.0,
                   value, side, "TQCB"))


def test_tape_across_isins_newest_first(ta):
    _add(ta, "RU000A0000A1", 1, 0, 1_000, hour=10)
    _add(ta, "RU000A0000B2", 2, 0, 2_000, hour=15)
    _add(ta, "RU000A0000C3", 3, 5, 3_000)

    rows = ta.read_tape(frm=_iso(0))
    assert [r["isin"] for r in rows] == ["RU000A0000B2", "RU000A0000A1"]  # новые сверху


def test_tape_filters(ta):
    _add(ta, "RU000A0000A1", 1, 0, 500_000, side="buy")
    _add(ta, "RU000A0000B2", 2, 0, 5_000_000, side="sell")

    assert len(ta.read_tape(frm=_iso(0), min_value=1_000_000)) == 1
    assert len(ta.read_tape(frm=_iso(0), side="buy")) == 1
    assert len(ta.read_tape(frm=_iso(0), isins=["RU000A0000B2"])) == 1
    assert len(ta.read_tape(frm=_iso(0), isins=["RU000A0000A1", "RU000A0000B2"])) == 2


def test_stats_count_whole_window_not_page(ta):
    """Лимит режет строки, но не итоги: иначе оборот занижался бы на хвост."""
    for i in range(10):
        _add(ta, "RU000A0000A1", i, 0, 1_000, hour=10 + i % 8)

    rows = ta.read_tape(frm=_iso(0), limit=3)
    stats = ta.tape_stats(frm=_iso(0))
    assert len(rows) == 3
    assert stats["n"] == 10
    assert stats["value"] == pytest.approx(10_000)


def test_stats_sides_and_top(ta):
    _add(ta, "RU000A0000A1", 1, 0, 7_000, side="buy")
    _add(ta, "RU000A0000A1", 2, 0, 1_000, side="sell")
    _add(ta, "RU000A0000B2", 3, 0, 2_000, side="sell")

    s = ta.tape_stats(frm=_iso(0), top=2)
    assert s["buy_value"] == pytest.approx(7_000)
    assert s["sell_value"] == pytest.approx(3_000)
    assert [t["isin"] for t in s["issuers_top"]] == ["RU000A0000A1", "RU000A0000B2"]


def test_stats_respect_same_filter_as_rows(ta):
    """Итоги и строки обязаны считаться по ОДНОМУ фильтру."""
    _add(ta, "RU000A0000A1", 1, 0, 500_000)
    _add(ta, "RU000A0000B2", 2, 0, 5_000_000)

    rows = ta.read_tape(frm=_iso(0), min_value=1_000_000)
    s = ta.tape_stats(frm=_iso(0), min_value=1_000_000)
    assert len(rows) == s["n"] == 1
    assert s["value"] == pytest.approx(5_000_000)
