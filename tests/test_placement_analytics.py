"""Регрессии первички: реальные SQLite-запросы, без сети и рабочих БД."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from services import portfolio_db as db, placement_analytics as pa
from services import primary_placements as pp, instruments_registry as reg


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "primary.db")
    db.init_db()
    monkeypatch.setattr(reg, "labels_map", lambda *a: {"TEST": {"base": "KEYRATE"}})
    monkeypatch.setattr(reg, "pricing_revisions", lambda: {"TEST": "v1"})
    return db._connect


def place(storage, day="2026-08-01", price=100, volume=100, board="PRIM"):
    with storage() as c:
        c.execute("INSERT INTO placement_day(secid,isin,date,board,price,volume) "
                  "VALUES('TEST','TEST',?,?,?,?)", (day, board, price, volume))


def trade(storage, day="2026-08-31", price=101, board="TQCB", value=1000, numtrades=1):
    with storage() as c:
        c.execute("INSERT INTO bond_day(isin,date,board,close,value,numtrades) "
                  "VALUES('TEST',?,?,?,?,?)", (day, board, price, value, numtrades))


def test_first_day_price_independent_of_later_placement(storage):
    place(storage)
    place(storage, "2026-08-02", 98)
    r = pp.aggregates()[0]
    assert r["first_price"] == 100
    assert r["wa_price"] == 99
    place(storage, "2026-08-01", 102, 300, "PRM2")
    assert pp.aggregates()[0]["first_price"] == 101.5


def test_missing_price_does_not_dilute_weighted_average(storage):
    place(storage, price=None)
    place(storage, "2026-08-02", 98)
    r = pp.aggregates()[0]
    assert r["first_price"] is None
    assert r["wa_price"] == 98


def test_after_point_requires_trades_and_selects_main_board(storage):
    trade(storage, price=999, numtrades=0)
    trade(storage, "2026-08-30", 101, value=100)
    trade(storage, "2026-08-30", 102, "TQOB", 200)
    trade(storage, "2026-09-01", 103, value=900)
    assert pa._after_point("TEST", "2026-08-01") == {
        "date": "2026-08-30", "price": 102, "board": "TQOB"}


def test_retry_cooldown_and_registry_changes(storage, monkeypatch):
    place(storage)
    inputs = pa._input_rows()
    pa.save_metrics([{**inputs[0], "calc_status": "retry", "err": "TimeoutError"}])
    assert pa.pending(60) == []
    with storage() as c:
        c.execute("UPDATE placement_metrics SET calc_at=?", (
            (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    assert pa.pending(60) == ["TEST"]
    pa.save_metrics([{**inputs[0], "calc_status": "unsupported"}])
    assert pa.pending(60) == []
    monkeypatch.setattr(reg, "pricing_revisions", lambda: {"TEST": "v2"})
    assert pa.pending(60) == ["TEST"]


def test_old_versions_not_served_and_get_recomputed(storage):
    place(storage)
    pa.save_metrics([{**pa._input_rows()[0], "y_idx_bps": 999}])
    with storage() as c:
        c.execute("UPDATE placement_metrics SET engine_ver=1")
    assert pa.metrics_map(["TEST"]) == {}
    assert pa.pending(60) == ["TEST"]


@pytest.mark.parametrize("after_kind,after_date,mode,expected,reason", [
    ("call", date(2027, 1, 1), "market", -20, None),
    ("maturity", date(2027, 1, 1), "market", None, "горизонт"),
    ("call", date(2027, 2, 1), "market", None, "горизонт"),
    ("call", date(2027, 1, 1), "realized", None, "реконструирована"),
])
def test_compute_same_horizon_and_both_curves(storage, monkeypatch,
        after_kind, after_date, mode, expected, reason):
    from services import backdate
    place(storage)
    place(storage, "2026-08-02", 98)
    trade(storage)
    prices = []

    async def ctx(isin, d, board=None):
        return {"date": d, "curve_mode": "market",
                "ruonia_curve_mode": mode if d.day == 31 else "market"}

    def reprice(context, price):
        prices.append(price)
        start = context["date"].day == 1
        return {"preferred_horizon": "call" if start else "maturity", "horizons": {
            "call" if start else after_kind: {
                "date": date(2027, 1, 1) if start else after_date,
                "yield_over_index_bps": 200 if start else 180}}}

    monkeypatch.setattr(backdate, "load_backdate_ctx", ctx)
    monkeypatch.setattr(backdate, "reprice_asof", reprice)
    assert asyncio.run(pa.compute())["priced"] == 1
    assert prices == [100, 101]
    r = pa.metrics_map()["TEST"]
    assert r["premium_bps"] == expected
    assert r["horizon"] == "call" and r["horizon_date"] == "2027-01-01"
    if reason:
        assert reason in r["after_err"]
    else:
        assert r["after_err"] is None
    # Пакетный CLI должен завершаться, а не выбирать тот же выпуск повторно.
    assert asyncio.run(pa.compute())["done"] == 0


def test_secondary_failure_preserves_start_and_is_retryable(storage, monkeypatch):
    from services import backdate
    place(storage)
    trade(storage)

    async def ctx(isin, d, board=None):
        if d.day == 31:
            raise TimeoutError("temporary")
        return {"curve_mode": "market", "ruonia_curve_mode": "market"}

    monkeypatch.setattr(backdate, "load_backdate_ctx", ctx)
    monkeypatch.setattr(backdate, "reprice_asof", lambda *a: {
        "horizons": {"maturity": {"date": date(2027, 1, 1), "yield_over_index_bps": 200}}})
    asyncio.run(pa.compute())
    r = pa.metrics_map()["TEST"]
    assert r["y_idx_bps"] == 200 and r["premium_bps"] is None
    assert r["err"] is None and "TimeoutError" in r["after_err"]
    assert r["calc_status"] == "retry"


def test_debut_uses_first_price(storage, monkeypatch):
    from api.routes.primary import get_placements
    place(storage)
    place(storage, "2026-08-02", 98)
    trade(storage, "2026-08-02", 100)
    result = asyncio.run(get_placements(date_from=None, date_to=None, q=None,
                                       min_rub=0, active=False, limit=100))
    assert result["rows"][0]["debut_pct"] == 0


def test_migration_preserves_old_rows(storage):
    with storage() as c:
        # Именно схема предыдущей версии, а не повтор init на новой таблице.
        c.execute("DROP TABLE placement_metrics")
        c.execute("CREATE TABLE placement_metrics(secid TEXT PRIMARY KEY, isin TEXT, "
                  "place_date TEXT, price REAL, y_idx_bps REAL, dm_bps REAL, curve_mode TEXT, "
                  "after_date TEXT, after_y_idx_bps REAL, premium_bps REAL, engine_ver INTEGER, "
                  "calc_at TEXT, err TEXT)")
        c.execute("INSERT INTO placement_metrics(secid,engine_ver,y_idx_bps) VALUES('OLD',1,200)")
    db.init_db()
    db.init_db()
    with storage() as c:
        r = dict(c.execute("SELECT * FROM placement_metrics WHERE secid='OLD'").fetchone())
    assert r["y_idx_bps"] == 200 and r["horizon"] is None


def test_old_success_can_be_refreshed_after_history_backfill(storage):
    place(storage, day="2025-01-01")
    pa.save_metrics([{**pa._input_rows()[0], "calc_status": "ok", "y_idx_bps": 200}])
    assert pa.pending(60) == []
    with storage() as c:
        c.execute("UPDATE placement_metrics SET calc_at=?", (
            (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),))
    assert pa.pending(60) == ["TEST"]


def test_market_slices_exclude_reconstructed_spreads(storage):
    place(storage, day=date.today().isoformat())
    pa.save_metrics([{**pa._input_rows()[0], "y_idx_bps": 999, "curve_mode": "realized"}])
    result = pa.market_slices()
    assert result["months"][0]["spread_med_bps"] is None
    assert result["months"][0]["priced"] == 0


def test_reprice_origin_includes_ruonia_curve(monkeypatch):
    from services import backdate, valuation
    monkeypatch.setattr(valuation, "calculate_valuation_metrics", lambda *a, **kw: {})
    ctx = dict(ref_obj=None, curve=None, date=date(2026, 8, 1), accrued=0,
               periods=[], amorts=[], offers=[], ctx_warnings=[], curve_mode="market",
               ruonia_curve_mode="realized")
    assert backdate.reprice_asof(ctx, 100)["curve_mode"] == "realized"
    ctx["ruonia_curve_mode"] = "market"
    assert backdate.reprice_asof(ctx, 100)["curve_mode"] == "market"
