#!/usr/bin/env python3
"""Разовый налив истории размещений (борды «Размещение» MOEX).

Ночной такт API добирает только хвост в 10 дней — глубина заливается отсюда:

    python3 scripts/backfill_placements.py            # год назад
    python3 scripts/backfill_placements.py --days 1095
    python3 scripts/backfill_placements.py --force    # перечитать собранное

Стоимость: одна дата = один запрос ISS на каждый борд размещения (их три),
год ≈ 1100 запросов и несколько минут. Идемпотентно — сходившие даты
пропускаются по отметке в placement_sync.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.portfolio_db import init_db          # noqa: E402
from services import primary_placements as pp      # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=pp.BACKFILL_DAYS)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-sec-ref", action="store_true",
                    help="не обновлять справочник эмитентов (15,7 тыс. бумаг, ~35 с)")
    a = ap.parse_args()

    init_db()
    if not a.no_sec_ref:
        print("справочник рынка:", await pp.sync_sec_ref())
    print("размещения:", await pp.backfill(days=a.days, force=a.force))
    print("итого:", pp.stats())


if __name__ == "__main__":
    asyncio.run(main())
