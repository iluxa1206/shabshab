#!/usr/bin/env python
"""Прод-сверка: будущие купоны карточки vs дневная раскладка фиксинга (/audit).

Для контрольных бумаг сравнивает по каждому будущему купону ставку карточки
(display_rate_pct из канонического cashflow) с projected_ks_pct дневной
раскладки + маржа. KEYRATE обязан сходиться ~в ноль; RUONIA — прогнозный хвост
осознанно расходится (par-конвенция daily-comp), помечаем справочно.

Запускается ВНУТРИ контейнера (см. scripts/prod_apply_fixing_specs.sh --verify-conv).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BONDS = [
    "RU000A108QA3",   # Мегафон2P4 — KEYRATE average·5
    "RU000A102QL3",   # KEYRATE point·5
    "RU000A106K43",   # average·38 (бывший avg_prev)
    "RU000A10D1H3",   # РЖД 1Р-46R — RUONIA average·37 (хвост расходится осознанно)
]


async def main() -> int:
    from services.market_data import MarketDataService
    from services.paths import cache_path
    from services.bond_audit import coupon_day_rates

    cache = MarketDataService.get_local_bond_cache(cache_path("isins_cache.json"))
    worst_ks = 0.0
    for isin in BONDS:
        try:
            dr = await coupon_day_rates(isin, cache)
        except Exception as e:
            print(f"{isin}: ошибка {type(e).__name__}: {e}")
            continue
        spec = dr["spec"]
        margin = (spec.get("margin_bps") or 0) / 100.0
        print(f"\n{isin} [{dr['base']}] спека {spec['mode']}·{spec['lag']} маржа {margin}%")
        for g in dr["coupons"][:4]:
            disp, proj = g.get("display_rate_pct"), g.get("projected_pct")
            if disp is None or proj is None:
                continue
            diff = disp - (proj + margin)
            mark = "OK" if abs(diff) < 0.01 else ("расходится (RUONIA-конвенция)"
                                                  if dr["base"] == "RUONIA" else "FAIL")
            if dr["base"] == "KEYRATE":
                worst_ks = max(worst_ks, abs(diff))
            print(f"  {g['start']}→{g['end']}: карточка {disp} vs спека+маржа "
                  f"{round(proj + margin, 4)} | Δ {round(diff, 4)}пп {mark}")
    print(f"\nмакс |Δ| по KEYRATE: {round(worst_ks, 4)}пп")
    return 0 if worst_ks < 0.01 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
