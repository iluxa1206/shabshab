#!/usr/bin/env python3
"""Бэкфилл дневных снимков ФИКСОВ (spread_daily kind='fixed') за дни, когда
вечерний снимок не писался.

    python3 scripts/backfill_fixed_snapshots.py --from 2026-08-29 --to 2026-09-08
    python3 scripts/backfill_fixed_snapshots.py --from ... --to ... --all     # весь универс фиксов
    python3 scripts/backfill_fixed_snapshots.py --from ... --to ... --force   # перезаписать snap-дни

Откуда числа: цена дня — bond_day биржи (стакан впереди прочих бордов,
средневзвес впереди закрытия, как в /api/fixed/ofz/asof), НКД — по графику
купонов на дату поставки, YTM/дюрация/g-спред — тем же compute_fixed_row,
что и витрина, с КБД того дня из архива gcurve_daily (при промахе — ISS).
Строки помечаются src='backfill': это репрайс по дневной цене, а не снимок
живого движка — as-of витрина и премия аукционов их читают наравне, сверки
(src='snap') — нет.

По умолчанию только ОФЗ (cls='ofz'): ради них и делается — режим ВЧЕРА на
/fixed/ofz и премия аукционов к вторичке. --all считает весь универс (~750
бумаг × дни, bondization на холодном кэше — минуты).

Дни без единой цены в bond_day (выходные) пропускаются молча. Дни, где snap
уже есть, — тоже (иначе --force).
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.portfolio_db import init_db, _connect, _lock     # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_fixed_snapshots")

_STEP_BACK_GCURVE = 5


def _day_prices(d: str, isins: list) -> dict:
    """{isin: цена} за день из bond_day — правило приоритета бордов то же, что
    у as-of витрины ОФЗ."""
    from services import block_trades as bt
    px: dict = {}
    for r in bt.read_bond_days(d, isins):
        p = r.get("waprice") or r.get("close")
        if p is None:
            continue
        rank = (0 if bt.classify_board(r.get("board")) == "book" else 1, -(r.get("value") or 0.0))
        cur = px.get(r["isin"])
        if cur is None or rank < cur[0]:
            px[r["isin"]] = (rank, float(p))
    return {k: v[1] for k, v in px.items()}


def _has_snap(d: str) -> bool:
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM spread_daily WHERE date=? AND kind='fixed' AND src='snap' LIMIT 1",
            (d,)).fetchone())


def _write(rows: list) -> int:
    with _lock, _connect() as c:
        c.executemany(
            "INSERT OR REPLACE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
            "g_spread_bps,z_bps,ytm,y_idx,src,horizon,y_idx_alt,alt_horizon) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


async def _gcurve_on(d: date):
    """КБД на день (шаг назад до 5 дней) или None — g-спред тогда прочерк."""
    from services.market_data import MarketDataService
    from services.zspread import GCurve
    for k in range(_STEP_BACK_GCURVE + 1):
        pts = await MarketDataService.get_gcurve_points_on((d - timedelta(days=k)).isoformat())
        if pts and len(pts) >= 2:
            g = GCurve(pts)
            return g if g.ok() else None
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--all", action="store_true", help="весь универс фиксов, не только ОФЗ")
    ap.add_argument("--force", action="store_true", help="считать и дни, где snap уже есть")
    a = ap.parse_args()
    d0, d1 = date.fromisoformat(a.d_from), date.fromisoformat(a.d_to)
    if d1 < d0:
        sys.exit("--to раньше --from")

    init_db()
    from services import fixed_income as fi
    from services.market_data import MarketDataService
    from core.valuation import settle_date, accrued_at, accrued_estimate

    uni = await fi.fetch_fixed_universe()
    if not a.all:
        uni = [u for u in uni if u.get("cls") == "ofz"]
    uni = [u for u in uni if u.get("isin")]
    isins = [u["isin"] for u in uni]
    print(f"универс: {len(uni)} бумаг ({'все фиксы' if a.all else 'ОФЗ'})")

    # расписания по SECID: ISIN ОФЗ в bondization не резолвится (правило
    # compute_fixed_metrics_all)
    fulls = await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(u.get("secid") or u["isin"]) for u in uni),
        return_exceptions=True)
    sched = {u["isin"]: ({} if isinstance(f, Exception) else (f or {})) for u, f in zip(uni, fulls)}
    print(f"расписаний: {sum(1 for f in sched.values() if f.get('coupons'))}")

    total = 0
    d = d0
    while d <= d1:
        ds = d.isoformat()
        d += timedelta(days=1)
        if not a.force and _has_snap(ds):
            print(f"{ds}: snap уже есть — пропуск")
            continue
        px = await asyncio.to_thread(_day_prices, ds, isins)
        if not px:
            continue
        g = await _gcurve_on(date.fromisoformat(ds))
        calc = date.fromisoformat(ds)
        settle = settle_date(calc)
        rows, errs = [], 0
        for u in uni:
            isin = u["isin"]
            full = sched.get(isin) or {}
            price = px.get(isin)
            if price is None or not full.get("coupons"):
                continue
            row = dict(u)
            acc = accrued_at(full["coupons"], settle)
            if acc is None:
                acc = accrued_estimate(full["coupons"], settle)
            row["accrued"] = float(acc) if acc is not None else float(u.get("accrued") or 0.0)
            try:
                m = fi.compute_fixed_row(row, full, g, calc, price_override=float(price))
            except Exception as e:
                errs += 1
                log.warning("%s %s: %s", ds, isin, e)
                continue
            if m.get("ytm") is None:
                continue
            rows.append((isin, ds, "fixed", float(price), None,
                         m.get("g_spread_bps"), m.get("z_spread_bps"), m.get("ytm"), None,
                         "backfill", m.get("horizon"), None, None))
        n = await asyncio.to_thread(_write, rows) if rows else 0
        total += n
        print(f"{ds}: цен {len(px)}, записано {n}, ошибок {errs}, КБД {'есть' if g else 'нет'}")
    print(f"итого записано: {total}")


if __name__ == "__main__":
    asyncio.run(main())
