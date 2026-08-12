#!/usr/bin/env python3
"""Разовый прогон калибровки базы/маржи по всему хвосту реестра.

В синке этот шаг идёт порциями (_MAX_INFER_PER_RUN), чтобы не растягивать
прогон; здесь — весь пул «флоатер без базы» за раз, с параллельным чтением
bondization. Тем же проходом фикс-купонные бумаги, ошибочно принятые discovery
за флоатеры, уходят в base='FIXED' (см. coupon_calib.looks_fixed_coupons).

После записи параметров сделки этих бумаг возвращаются в очередь расчёта
спреда: они уже были закрыты прочерком как «не флоатер» и сами не вернулись бы.

    python3 scripts/backfill_infer_params.py [--dry] [--limit N]
"""
import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import coupon_calib as cc                    # noqa: E402
from services import instruments_registry as reg           # noqa: E402
from services.market_data import MarketDataService         # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="только показать, что бы сделал")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число бумаг")
    args = ap.parse_args()

    today = date.today()
    targets = [r["isin"] for r in reg.list_incomplete() if r["base"] is None]
    if args.limit:
        targets = targets[:args.limit]
    print(f"кандидатов (флоатер без базы): {len(targets)}")

    sem = asyncio.Semaphore(6)
    fixed: list[tuple] = []
    filled: list[tuple] = []
    skipped: list[tuple] = []

    async def one(isin: str) -> None:
        row = reg.get(isin) or {}
        async with sem:
            try:
                full = await MarketDataService.fetch_bond_schedule_full(isin)
            except Exception as e:
                skipped.append((isin, row.get("short_name"), f"нет графика: {e}"))
                return
        coupons = (full or {}).get("coupons") or []
        amorts = (full or {}).get("amortizations") or []
        face = row.get("face_value") or 1000.0
        fx = cc.looks_fixed_coupons(coupons, face, today, amorts)
        if fx:
            fixed.append((isin, row.get("short_name"), fx))
            return
        spec, why = cc.infer_base_margin(coupons, face, today, amorts)
        (filled if spec else skipped).append((isin, row.get("short_name"), spec or why))

    await asyncio.gather(*[one(i) for i in targets])

    print(f"\nФИКС (ошибочно заведены как флоатеры): {len(fixed)}")
    for i, n, v in fixed:
        print(f"  {i} {n or '':<12} ставка {v['rate']}% · КС ходила на {v['ks_span_pp']}пп · {v['n']} купонов")
    print(f"\nБАЗА И МАРЖА ОПРЕДЕЛЕНЫ: {len(filled)}")
    for i, n, s in filled:
        print(f"  {i} {n or '':<12} {s['base']} +{s['margin_bps']}бп · err {s['err_pp']}пп · "
              f"{s['n']} купонов · разброс индекса {s['span_pp']}пп")
    print(f"\nОСТАЛИСЬ БЕЗ ПАРАМЕТРОВ: {len(skipped)}")

    if args.dry:
        print("\n--dry: ничего не записано")
        return 0

    for isin, _n, _v in fixed:
        reg.reclassify_fixed(isin)
        reg.mark_enrich_attempt(isin, "filled")
    for isin, _n, s in filled:
        reg.upsert({"isin": isin, "base": s["base"], "margin_bps": s["margin_bps"]},
                   source="coupon-calib")
        reg.mark_enrich_attempt(isin, "filled")
    print(f"\nзаписано: {len(fixed)} фиксов, {len(filled)} флоатеров с параметрами")

    if filled:
        from services import block_trades as bt
        n = await asyncio.to_thread(bt.reset_metrics, [i for i, _n, _s in filled])
        print(f"сделок возвращено в очередь расчёта спреда: {n}")
        left = await bt.price_new_trades(limit=len(filled) + 20)
        print(f"досчитано сразу: {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
