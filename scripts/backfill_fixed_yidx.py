#!/usr/bin/env python3
"""Бэкфилл истории Y-IDX ФИКСОВ в spread_daily.y_idx за прошлые дни.

    python3 scripts/backfill_fixed_yidx.py --from 2026-07-30 --to 2026-10-13 --dry
    python3 scripts/backfill_fixed_yidx.py --from 2026-07-30 --to 2026-10-13 --all

Зачем: живой Y-IDX фиксов копится вперёд только с 06.10.2026, а график во
времени (Сравнение, динамика) нужен глубже. Смешивать G-спред (история) и
Y-IDX (живое) на одной линии нельзя, поэтому ряд Y-IDX достраивается назад.

Откуда числа — как у backfill_fixed_snapshots.py (цена дня из bond_day, КБД дня,
график MOEX, НКД на дату поставки), плюс RUONIA-кривая НА ДЕНЬ из
backdate.curve_asof: с 30.07.2026 — архив своп-котировок (mode "market"), раньше —
гибрид «факт индекса + первая архивная кривая» ("realized", ОЦЕНКА). Режим
печатается по каждому дню.

Что пишет: ТОЛЬКО колонку y_idx. Существующая строка с y_idx не трогается
(живой снимок и прежний бэкфилл точнее); NULL дополняется; строки нет — создаётся
src='backfill' с теми же g/ytm, что у snapshots-бэкфилла. Идемпотентен.

--dry ничего не пишет и сверяет посчитанное с уже лежащим в базе y_idx (дни
после 06.10, где его писал живой движок) — это проверка методики на фактах.
"""
import argparse
import asyncio
import logging
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.portfolio_db import init_db, _connect, _lock     # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_fixed_yidx")


def _existing(ds: str) -> dict:
    """{isin: (y_idx или None, price_pct)} — что уже лежит у фиксов на день."""
    with _connect() as c:
        return {r["isin"]: (r["y_idx"], r["price_pct"]) for r in c.execute(
            "SELECT isin, y_idx, price_pct FROM spread_daily WHERE date=? AND kind='fixed'", (ds,))}


def _write(rows: list) -> int:
    """UPSERT только колонки y_idx: существующее число не перетирается."""
    with _lock, _connect() as c:
        c.executemany(
            "INSERT INTO spread_daily(isin,date,kind,price_pct,dm_bps,g_spread_bps,z_bps,"
            "ytm,y_idx,src) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(isin,date) DO UPDATE SET y_idx=excluded.y_idx "
            "WHERE spread_daily.y_idx IS NULL", rows)
    return len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--all", action="store_true", help="весь универс фиксов, не только ОФЗ")
    ap.add_argument("--dry", action="store_true", help="не писать; сверить с имеющимся y_idx")
    a = ap.parse_args()
    d0, d1 = date.fromisoformat(a.d_from), date.fromisoformat(a.d_to)
    if d1 < d0:
        sys.exit("--to раньше --from")

    init_db()
    import backfill_fixed_snapshots as bs
    from services import fixed_income as fi
    from services.backdate import curve_asof
    from services.market_data import MarketDataService
    from services.valuation import _index_provider
    from core.valuation import settle_date, accrued_at, accrued_estimate

    uni = await fi.fetch_fixed_universe()
    if not a.all:
        uni = [u for u in uni if u.get("cls") == "ofz"]
    uni = [u for u in uni if u.get("isin")]
    isins = [u["isin"] for u in uni]
    print(f"универс: {len(uni)} бумаг ({'все фиксы' if a.all else 'ОФЗ'})")

    ruonia, _ks, _cd, _rd = await MarketDataService.get_curves()
    if ruonia is None:
        sys.exit("RUONIA-кривая сегодня недоступна — as-of сшить не из чего")
    ru_hist = _index_provider("RUONIA", [], None)[1]
    if not ru_hist:
        sys.exit("история RUONIA недоступна — гибрид собрать не из чего")

    fulls = await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(u.get("secid") or u["isin"]) for u in uni),
        return_exceptions=True)
    sched = {u["isin"]: ({} if isinstance(f, Exception) else (f or {})) for u, f in zip(uni, fulls)}
    print(f"расписаний: {sum(1 for f in sched.values() if f.get('coupons'))}")

    total = 0
    d = d0
    while d <= d1:
        ds = d.isoformat()
        calc = d
        d += timedelta(days=1)
        px = await asyncio.to_thread(bs._day_prices, ds, isins)
        if not px:
            continue
        try:
            ru_curve, mode = curve_asof("RUONIA", calc, ruonia, ru_hist)
        except Exception as e:
            print(f"{ds}: RUONIA-кривая не собралась ({e}) — день пропущен")
            continue
        g = await bs._gcurve_on(calc)
        settle = settle_date(calc)
        have = await asyncio.to_thread(_existing, ds)
        rows, errs, cmp_ = [], 0, []
        for u in uni:
            isin = u["isin"]
            full = sched.get(isin) or {}
            price = px.get(isin)
            if price is None or not full.get("coupons"):
                continue
            row = dict(u)
            acc = accrued_at(full["coupons"], settle)
            if acc is None:
                # accrued_estimate ждёт кортежи (начало, конец, сумма), не словари
                try:
                    per = [(date.fromisoformat(c["start"]), date.fromisoformat(c["end"]),
                            c.get("value")) for c in full["coupons"]
                           if c.get("start") and c.get("end")]
                    acc = accrued_estimate(per, settle)
                except Exception:
                    acc = None
            row["accrued"] = float(acc) if acc is not None else float(u.get("accrued") or 0.0)
            try:
                m = fi.compute_fixed_row(row, full, g, calc, price_override=float(price),
                                         ru_curve=ru_curve)
            except Exception as e:
                errs += 1
                log.warning("%s %s: %s", ds, isin, e)
                continue
            y = m.get("yield_over_index_bps")
            if y is None or m.get("ytm") is None:
                continue
            old, old_px = have.get(isin, (None, None))
            if old is not None:
                # сверка с живым движком; same_px — цена снимка совпала с ценой дня
                # (тогда расхождение — чистая методика, а не база цены)
                cmp_.append((float(y) - float(old),
                             old_px is not None and abs(float(old_px) - float(price)) < 0.011))
                continue                                  # существующее не трогаем
            rows.append((isin, ds, "fixed", float(price), None, m.get("g_spread_bps"),
                         m.get("z_spread_bps"), m.get("ytm"), float(y), "backfill"))
        n = 0
        if rows and not a.dry:
            n = await asyncio.to_thread(_write, rows)
        total += n
        cmp_txt = ""
        if cmp_:
            same = [x for x, ok in cmp_ if ok]
            cmp_txt = (f" | сверка с живым y_idx: n={len(cmp_)}, медиана Δ={statistics.median(x for x, _ in cmp_):+.1f} бп, "
                       f"|Δ|={statistics.median(abs(x) for x, _ in cmp_):.1f}")
            if same:
                cmp_txt += (f" | при ТОЙ ЖЕ цене n={len(same)}: Δ={statistics.median(same):+.1f}, "
                            f"|Δ|={statistics.median(abs(x) for x in same):.1f}, "
                            f"p90|Δ|={sorted(abs(x) for x in same)[int(0.9 * (len(same) - 1))]:.0f}")
        print(f"{ds} [{mode}]: цен {len(px)}, к записи {len(rows)}, записано {n}, "
              f"ошибок {errs}, КБД {'есть' if g else 'нет'}{cmp_txt}")
    print(f"итого записано: {total}" + (" (dry-run)" if a.dry else ""))


if __name__ == "__main__":
    asyncio.run(main())
