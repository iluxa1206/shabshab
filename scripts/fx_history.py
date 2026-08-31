#!/usr/bin/env python3
"""Архив курсов валют по дням: наливка истории и пересчёт объёмов по нему.

Зачем архив. Цена облигации — процент от номинала, а у замещающих и юаневых
выпусков номинал в валюте. Рублёвый объём сделки за ПРОШЛУЮ дату считается
курсом ТОГО дня: USD 03.08.2026 стоил 80,24 против 85,84 на 31.08 — единый
сегодняшний курс завышал объём всего окна на движение валюты (замер: пересчёт
окна 11–31.08 биржевым VALUE отнял 1,59 млрд ₽).

Живой слой (services/fx) знал только «сейчас». Теперь курс дня фиксируется в
таблице fx_rate: вперёд — самим слоем при каждом обновлении, назад — этим
скриптом (история MOEX TOM, недостающие валюты — ЦБ).

    python scripts/fx_history.py --days 400            # налить историю
    python scripts/fx_history.py --repair --days 30    # + пересчитать тики
    python scripts/fx_history.py --repair --dry-run

--repair пересчитывает только те тики, у которых НЕТ биржевого двойника в
block_trade: где двойник есть, объём ставится по VALUE самой биржи
(scripts/fix_tick_values.py) — это точнее любого нашего пересчёта.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import fx  # noqa: E402
from services import block_trades as bt  # noqa: E402
from services import trades_archive as ta  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="глубина истории курсов")
    ap.add_argument("--repair", action="store_true",
                    help="пересчитать объёмы валютных тиков по курсу дня")
    ap.add_argument("--repair-days", type=int, default=30, help="окно пересчёта")
    ap.add_argument("--tol", type=float, default=0.01)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    res = await fx.backfill_history(a.days)
    print(f"курсы: записано {res['saved']} значений за {res['from']}…{res['till']}")
    for ccy, st in (res.get("archive") or {}).items():
        print(f"  {ccy}: {st['n']} дней, {st['from']}…{st['till']}")

    if a.repair:
        # 1) суммы валютных бордов в block_trade — к рублям по курсу дня
        cur = await bt.repair_currency_values(days=a.repair_days, dry_run=a.dry_run)
        print(f"валютные борды: {'нашлось' if a.dry_run else 'исправлено'} "
              f"{cur['rows']} из {cur.get('seen', 0)} строк, "
              f"сдвиг {cur['delta']/1e9:.2f} млрд ₽"
              + (f", пропущено {cur['skipped']}" if cur.get("skipped") else ""))
        # 2) тики, у которых есть биржевой двойник, — по его VALUE
        if not a.dry_run:
            fixed = ta.repair_values(days=a.repair_days, tol=a.tol)
            print(f"тики по бирже: исправлено {fixed['rows']}, "
                  f"сдвиг {fixed['delta']/1e9:.2f} млрд ₽")
        # 3) остальные — полным пересчётом по номиналу и курсу дня
        rep = await ta.repair_fx_values(days=a.repair_days, tol=a.tol,
                                        dry_run=a.dry_run)
        verb = "нашлось" if a.dry_run else "пересчитано"
        print(f"объёмы: {verb} {rep['rows']} тиков по {rep['isins']} бумагам, "
              f"сдвиг {rep['delta']/1e9:.2f} млрд ₽"
              + (f", без курса {rep['skipped_no_rate']}" if rep["skipped_no_rate"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
