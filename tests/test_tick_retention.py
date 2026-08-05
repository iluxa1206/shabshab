"""Ретеншен тикового архива + обрезка окна обогащения баров.

Архив рос без границы (6.7 млн строк за месяц). Прун держит сырое окно целиком,
за ним — только крупные принты (их и показывает лента фронта). Тесты страхуют
инвариант, который легко потерять: пересчёт buy/sell VWAP не должен заходить за
сырое окно, иначе частичный агрегат затрёт верный.
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
    # вернуть модули на боевой DB_PATH для остальных тестов сессии
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(mod)


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _add_tick(ta, isin, tid, days_ago, value, hour=12, side="buy", qty=1.0):
    with ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (isin, tid, f"{_iso(days_ago)} {hour:02d}:00:00", 100.0, qty,
                   value, side, "TQCB"))


def _count(ta) -> int:
    with ta._connect() as c:
        return c.execute("SELECT COUNT(*) FROM trade_tick").fetchone()[0]


def test_raw_window_may_be_short_but_covers_drain_overlap(monkeypatch):
    """Короткое сырое окно разрешено — дрейн инкрементальный и удалённую мелочь
    заново не качает. Ниже нахлёста дрейна опускать нельзя: прун и дрейн начали
    бы качели «удалили ↔ скачали заново»."""
    import services.trades_archive as mod
    monkeypatch.setenv("TICK_RAW_DAYS", "7")
    importlib.reload(mod)
    assert mod.TICK_RAW_DAYS == 7

    monkeypatch.setenv("TICK_RAW_DAYS", "0")
    importlib.reload(mod)
    assert mod.TICK_RAW_DAYS >= mod.TICK_DRAIN_OVERLAP_HOURS / 24 + 1

    monkeypatch.delenv("TICK_RAW_DAYS", raising=False)
    importlib.reload(mod)
    assert mod.TICK_RAW_DAYS == 35


def test_prune_keeps_raw_window_whole(ta):
    _add_tick(ta, "RU000A100001", 1, days_ago=1, value=5_000)      # свежая мелочь
    _add_tick(ta, "RU000A100001", 2, days_ago=30, value=5_000)     # ещё в окне
    res = ta.prune(raw_days=35, big_value=1_000_000)
    assert res["deleted"] == 0
    assert _count(ta) == 2


def test_prune_drops_only_small_ticks_beyond_window(ta):
    _add_tick(ta, "RU000A100001", 1, days_ago=100, value=5_000)        # мелочь → снос
    _add_tick(ta, "RU000A100001", 2, days_ago=100, value=9_000_000)    # крупная → живёт
    _add_tick(ta, "RU000A100002", 3, days_ago=100, value=1_000_000)    # ровно порог → живёт
    _add_tick(ta, "RU000A100002", 4, days_ago=100, value=None)         # без объёма → снос
    res = ta.prune(raw_days=35, big_value=1_000_000)
    assert res["deleted"] == 2
    assert res["isins"] == 2
    with ta._connect() as c:
        left = sorted(r[0] for r in c.execute("SELECT trade_id FROM trade_tick"))
    assert left == [2, 3]


def test_prune_honours_short_window(ta):
    """raw_floor обязан слушаться КОРОТКОГО окна: пока он втихую зажимал его до
    глубины брокера, prune(raw_days=7) чистил по границе 30 дней."""
    _add_tick(ta, "RU000A100001", 1, days_ago=20, value=5_000)     # вне окна 7д
    _add_tick(ta, "RU000A100001", 2, days_ago=3, value=5_000)      # внутри
    assert ta.raw_floor(7) == _iso(7)
    res = ta.prune(raw_days=7, big_value=1_000_000)
    assert res["deleted"] == 1
    assert _count(ta) == 1


def test_prune_dry_run_deletes_nothing(ta):
    _add_tick(ta, "RU000A100001", 1, days_ago=100, value=5_000)
    res = ta.prune(raw_days=35, big_value=1_000_000, dry_run=True)
    assert res["deleted"] == 1 and res["dry_run"] is True
    assert _count(ta) == 1


def test_enrich_does_not_reach_beyond_raw_window(ta, monkeypatch):
    """Час за сырым окном уже обогащён, а из тиков там остались только крупные.
    Пересчёт по ним дал бы заниженные buy/sell — окно обязано обрезаться."""
    monkeypatch.setattr(ta, "TICK_RAW_DAYS", 35)
    isin, old_h = "RU000A100001", f"{_iso(100)} 12:00"
    with ta._connect() as c:
        c.execute("INSERT INTO bar_hourly(isin,ts,kind,face,trades,buy_volume,buy_vwap) "
                  "VALUES(?,?,?,?,?,?,?)", (isin, old_h, "floater", 1000.0, 42, 500.0, 100.5))
    _add_tick(ta, isin, 1, days_ago=100, value=9_000_000, qty=90.0)   # переживший принт

    assert ta.enrich_bars_with_ticks(isin, frm=_iso(365)) == 0
    with ta._connect() as c:
        row = c.execute("SELECT trades, buy_volume, buy_vwap FROM bar_hourly "
                        "WHERE isin=? AND ts=?", (isin, old_h)).fetchone()
    assert (row["trades"], row["buy_volume"], row["buy_vwap"]) == (42, 500.0, 100.5)


def test_enrich_still_works_inside_raw_window(ta, monkeypatch):
    monkeypatch.setattr(ta, "TICK_RAW_DAYS", 35)
    isin, h = "RU000A100001", f"{_iso(2)} 12:00"
    with ta._connect() as c:
        c.execute("INSERT INTO bar_hourly(isin,ts,kind,face) VALUES(?,?,?,?)",
                  (isin, h, "floater", 1000.0))
    _add_tick(ta, isin, 1, days_ago=2, value=100_000, qty=100.0, side="buy")
    _add_tick(ta, isin, 2, days_ago=2, value=50_000, qty=50.0, side="sell")

    assert ta.enrich_bars_with_ticks(isin, frm=_iso(30)) == 1
    with ta._connect() as c:
        row = c.execute("SELECT trades, buy_volume, sell_volume FROM bar_hourly "
                        "WHERE isin=? AND ts=?", (isin, h)).fetchone()
    assert (row["trades"], row["buy_volume"], row["sell_volume"]) == (2, 100.0, 50.0)
