#!/usr/bin/env python3
"""Разовый прогон аналитики первички: спред книги, премия, сверка анонсов.

Ночной такт считает пачками по 60 — на первом наливе истории (228 флоатеров
реестра за год) это неделя ночей, поэтому глубина прогоняется отсюда:

    python3 scripts/run_placement_analytics.py            # всё, что в очереди
    python3 scripts/run_placement_analytics.py --limit 50
    python3 scripts/run_placement_analytics.py --no-details

Считается пачками с паузой: каждая бумага — backdate-пересчёт с солвером, и
непрерывный прогон на живом сервере отбирает CPU у витрины.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.portfolio_db import init_db                # noqa: E402
from services import primary_placements as pp            # noqa: E402
from services import placement_analytics as pa           # noqa: E402

BATCH = 25


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 — пока очередь не кончится")
    ap.add_argument("--no-details", action="store_true",
                    help="не обновлять паспорта выпусков (объём эмиссии)")
    ap.add_argument("--pause", type=float, default=1.0, help="пауза между пачками, с")
    a = ap.parse_args()

    init_db()
    if not a.no_details:
        print("паспорта выпусков:", await pp.sync_sec_details(lazy_limit=1000))

    done = 0
    while True:
        n = min(BATCH, a.limit - done) if a.limit else BATCH
        stat = await pa.compute(limit=n)
        done += stat.get("done", 0)
        print(f"  посчитано {done}: {stat}", flush=True)
        if not stat.get("done") or (a.limit and done >= a.limit):
            break
        time.sleep(a.pause)

    print("сверка анонсов:", pa.match_announces())


if __name__ == "__main__":
    asyncio.run(main())
