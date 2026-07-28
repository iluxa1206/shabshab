"""Пересчёт coupon_period_days из ФАКТИЧЕСКОГО графика купонов по всему реестру.

ЗАЧЕМ. Историческое значение — номинальное round(365/freq): у месячных бумаг 30,
хотя реальный период 31 день (business-day adjustment + календарные месяцы).
Точное значение считает core.cashflow.coupon_period_from_coupons: интервал между
двумя последними купонами, фолбэк — размещение→первый купон.

Тот же расчёт вшит в дневной прогрев юниверса (services.universe.
compute_universe_metrics), но он покрывает только бумаги универса и ждёт своего
цикла поллера. Этот скрипт прогоняет ВЕСЬ активный реестр здесь и сейчас.

Графики берутся через MarketDataService.fetch_bond_schedule_full (дневной дисковый
кэш) — на прогретом кэше сети почти нет. manual_locked-строки пропускаются:
coupon_period_days входит в _MANUAL_FIELDS, ручная правка авторитетнее.

ЗАПУСК (в контейнере): dry-run по умолчанию, APPLY=1 — запись.
    docker compose -f docker-compose.prod.yml exec floaters \
        python scripts/backfill_coupon_period.py
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 floaters \
        python scripts/backfill_coupon_period.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.cashflow import coupon_period_from_coupons  # noqa: E402
from services import instruments_registry as reg  # noqa: E402
from services.market_data import MarketDataService  # noqa: E402

CONCURRENCY = 8


async def main() -> int:
    apply = os.environ.get("APPLY") == "1"
    rows = reg.universe_rows(only_floaters=False, only_priceable=False)
    rows = [r for r in rows if r.get("isin")]
    print(f"строк в реестре: {len(rows)}")

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict):
        isin = row["isin"]
        async with sem:
            try:
                full = await MarketDataService.fetch_bond_schedule_full(isin)
            except Exception:
                return None
        cps = (full or {}).get("coupons") or []
        cur = reg.get(isin) or {}
        if cur.get("manual_locked"):
            return ("locked", isin, None, cur.get("coupon_period_days"))
        cpd = coupon_period_from_coupons(cps, issue_date=cur.get("issue_date"),
                                         today=date.today())
        if not cpd or cpd <= 0:
            return ("нет графика", isin, None, cur.get("coupon_period_days"))
        old = cur.get("coupon_period_days")
        return ("изменить" if cpd != old else "совпало", isin, cpd, old)

    res = [x for x in await asyncio.gather(*(one(r) for r in rows)) if x]
    buckets: dict[str, list] = {}
    for kind, isin, new, old in res:
        buckets.setdefault(kind, []).append((isin, new, old))

    for k in ("совпало", "изменить", "нет графика", "locked"):
        print(f"  {k:12}: {len(buckets.get(k, []))}")

    changes = buckets.get("изменить", [])
    by_shift: dict[str, int] = {}
    for _, new, old in changes:
        by_shift[f"{old} -> {new}"] = by_shift.get(f"{old} -> {new}", 0) + 1
    print("\nсдвиги:")
    for k, v in sorted(by_shift.items(), key=lambda kv: -kv[1]):
        print(f"  {k:16} {v}")

    if not apply:
        print("\n[DRY-RUN] записи не было. APPLY=1 — применить.")
        return 0

    for isin, new, _old in changes:
        upd = {"isin": isin, "coupon_period_days": new}
        if new > 0:
            upd["coupons_per_year"] = max(1, round(365 / new))
        reg.upsert(upd, source="moex", mark_new=False)
    print(f"\nобновлено строк: {len(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
