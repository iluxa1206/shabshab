"""Средневзвес по неделям (services.bars.weekly) + аукционы ОФЗ той же недели.

Неделя — календарная ПН–ВС; цена и спред недели взвешены ОБОРОТОМ дня, спред —
только по дням, где он есть (и посчитан актуальным движком к текущему
горизонту). Аукционы цепляются к неделе по дате, премия в цене — средневзвес
недели минус ср.взв. цена аукциона (плюс — аукцион дешевле рынка).
"""
import importlib
from datetime import date

import pytest

from services.bars import BARS_METRICS_VERSION as _BV

TODAY = date(2026, 10, 5)          # понедельник


@pytest.fixture
def bars(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.bars as bars_mod
    import services.ofz_auctions as oa
    importlib.reload(bars_mod)
    importlib.reload(oa)
    monkeypatch.setattr(bars_mod, "_msk_today", lambda: TODAY)
    yield pdb, bars_mod
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(bars_mod)
    importlib.reload(oa)


def _day(d, wap, value, *, y=None, g=None, kind="floater", ver=_BV, trades=1,
         horizon=None, y_alt=None, alt_horizon=None):
    return {"isin": "RU_T", "date": d, "kind": kind, "wap_pct": wap, "value": value,
            "volume": value / 1000, "trades": trades, "y_idx_wap_bps": y,
            "g_spread_wap_bps": g, "metrics_ver": ver, "horizon": horizon,
            "y_idx_alt_wap_bps": y_alt, "alt_horizon": alt_horizon}


def test_price_weighted_by_value_and_monday_bucketing(bars):
    _, b = bars
    res = b.weekly_calc([
        _day("2026-09-29", 100.0, 1e6, y=200),     # ПН
        _day("2026-10-04", 98.0, 3e6, y=240),      # ВС — та же неделя
        _day("2026-10-05", 99.0, 2e6, y=220),      # ПН — новая неделя
    ], [], weeks=4, today=TODAY)
    wk = res["weeks"]
    assert [w["week"] for w in wk] == ["2026-10-05", "2026-09-28"]   # новые сверху
    old = wk[1]
    assert old["week_end"] == "2026-10-04"
    assert old["days"] == 2 and old["trades"] == 2 and old["value"] == 4e6
    assert old["wap_pct"] == pytest.approx((100 * 1 + 98 * 3) / 4)     # 98.5, не 99
    assert old["y_idx_wap_bps"] == pytest.approx((200 * 1 + 240 * 3) / 4)
    assert old["g_spread_wap_bps"] is None


def test_spread_weights_skip_null_and_stale_days(bars):
    """День без спреда (или спред прошлого движка) не размывает знаменатель:
    вес спреда — только оборот дней со спредом, цена — всех дней."""
    _, b = bars
    res = b.weekly_calc([
        _day("2026-09-29", 100.0, 1e6, y=200),
        _day("2026-09-30", 96.0, 3e6, y=None),            # спреда нет
        _day("2026-10-01", 96.0, 5e6, y=999, ver=_BV - 1),  # стейл-версия
    ], [], weeks=2, today=TODAY)
    w = res["weeks"][0]
    assert w["y_idx_wap_bps"] == 200.0
    assert w["wap_pct"] == pytest.approx((100 + 96 * 3 + 96 * 5) / 9)


def test_fixed_uses_g_spread(bars):
    _, b = bars
    res = b.weekly_calc([_day("2026-10-01", 95.0, 1e6, g=80, kind="fixed"),
                         _day("2026-10-02", 94.0, 1e6, g=120, kind="fixed")],
                        [], weeks=2, today=TODAY)
    w = res["weeks"][0]
    assert res["kind"] == "fixed"
    assert w["g_spread_wap_bps"] == 100.0 and w["y_idx_wap_bps"] is None


def test_horizon_switch_takes_alt_branch(bars):
    """Неделя переключения горизонта: день на старом горизонте берёт спред
    второй ветки, а не складывает спред к оферте со спредом к погашению."""
    _, b = bars
    res = b.weekly_calc([
        _day("2026-09-29", 100.0, 1e6, y=500, horizon="maturity",
             y_alt=150, alt_horizon="put"),
        _day("2026-09-30", 100.0, 1e6, y=170, horizon="put"),
    ], [], weeks=2, today=TODAY)
    assert res["horizon"] == "put"
    assert res["weeks"][0]["y_idx_wap_bps"] == 160.0


def test_window_and_zero_value_days(bars):
    _, b = bars
    res = b.weekly_calc([_day("2026-08-01", 90.0, 1e6),       # вне окна 2 недель
                         _day("2026-10-06", 99.0, 0)],        # день без оборота
                        [], weeks=2, today=TODAY)
    assert res["weeks"] == []


def _seed(pdb, daily, aucs):
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO bar_daily(isin,date,kind,wap_pct,value,volume,trades,"
            "g_spread_wap_bps,metrics_ver) VALUES(?,?,?,?,?,?,?,?,?)",
            [(r["isin"], r["date"], r["kind"], r["wap_pct"], r["value"], r["volume"],
              r["trades"], r["g_spread_wap_bps"], r["metrics_ver"]) for r in daily])
        c.executemany(
            "INSERT INTO ofz_auction(date,code,fmt,secid,isin,sec_type,wap_price,"
            "cut_price,wap_yield,cut_yield,placed_mln,demand_mln,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", aucs)
        c.execute("INSERT INTO sec_ref(secid,isin) VALUES('SU26999RMFS1','RU_T')")


def test_auctions_attached_with_premium(bars):
    pdb, b = bars
    _seed(pdb,
          [_day("2026-09-29", 98.0, 1e6, g=10, kind="fixed"),
           _day("2026-10-01", 99.0, 1e6, g=20, kind="fixed")],
          [
              # по ISIN
              ("2026-10-01", "26999RMFS", "auction", "SU26999RMFS1", "RU_T", "ОФЗ-ПД",
               98.1, 98.0, 14.2, 14.25, 25000.0, 40000.0, "ok"),
              # резолв ISIN не нашёл: в isin лежит SECID → ловим через sec_ref
              ("2026-10-01", "26999RMFS", "drpa", "SU26999RMFS1", "SU26999RMFS1", "ОФЗ-ПД",
               98.1, None, 14.2, None, 3000.0, None, "drpa"),
              # несостоявшийся — без премии
              ("2026-09-24", "26999RMFS", "auction", "SU26999RMFS1", "RU_T", "ОФЗ-ПД",
               None, None, None, None, 0.0, 5000.0, "failed"),
              # чужая бумага
              ("2026-10-01", "26111RMFS", "auction", "SU26111RMFS1", "RU_X", "ОФЗ-ПД",
               90.0, 90.0, 15.0, 15.0, 1.0, 1.0, "ok"),
          ])
    res = b.weekly("RU_T", weeks=3)
    wk = {w["week"]: w for w in res["weeks"]}
    # неделя аукциона без сделок всё равно попадает — ради самого аукциона
    assert set(wk) == {"2026-09-28", "2026-09-21"}
    cur = wk["2026-09-28"]
    assert cur["wap_pct"] == 98.5 and cur["g_spread_wap_bps"] == 15.0
    assert [(a["fmt"], a["status"]) for a in cur["auctions"]] == [("auction", "ok"),
                                                                  ("drpa", "drpa")]
    # вторичка недели 98.5, аукцион 98.1 → Минфин разместил на 0.4 п.п. дешевле
    assert cur["auctions"][0]["prem_pp"] == 0.4
    assert cur["auctions"][0]["placed_mln"] == 25000.0
    failed = wk["2026-09-21"]
    assert failed["wap_pct"] is None and failed["value"] is None
    assert failed["auctions"][0]["status"] == "failed"
    assert failed["auctions"][0]["prem_pp"] is None
