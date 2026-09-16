#!/usr/bin/env python3
"""Разовый налив аукционов ОФЗ с сайта Минфина (services/ofz_auctions).

Ночной такт API берёт только текущий и прошлый год; глубина — отсюда:

    python3 scripts/backfill_ofz_auctions.py --all            # все годы из индекса + все планы
    python3 scripts/backfill_ofz_auctions.py --years 2024 2025
    python3 scripts/backfill_ofz_auctions.py --plans-only
    python3 scripts/backfill_ofz_auctions.py --all --force    # перечитать уже скачанное

Стоимость: один запрос на индекс + по одному xlsx на год (6 файлов с 2021) +
индекс графиков и по странице на квартал (12 с IV кв. 2023). Идемпотентно:
файл, уже скачанный под этим именем, пропускается.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.portfolio_db import init_db, _connect   # noqa: E402
from services import ofz_auctions as oa               # noqa: E402


def _report() -> None:
    with _connect() as c:
        print("строк по годам:", [tuple(r) for r in c.execute(
            "SELECT substr(date,1,4) y, COUNT(*) FROM ofz_auction GROUP BY y ORDER BY y")])
        print("без SECID:", c.execute(
            "SELECT COUNT(DISTINCT code) FROM ofz_auction WHERE secid IS NULL").fetchone()[0])
        print("планы по кварталам:", [tuple(r) for r in c.execute(
            "SELECT quarter, SUM(amount_bln), COUNT(*) FROM ofz_auction_plan "
            "WHERE bucket != '' GROUP BY quarter ORDER BY quarter")])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="все годы из индекса Минфина")
    ap.add_argument("--years", type=int, nargs="*", help="конкретные годы")
    ap.add_argument("--plans-only", action="store_true")
    ap.add_argument("--results-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="перечитать уже скачанное")
    a = ap.parse_args()

    init_db()
    if not a.plans_only:
        print("итоги:", await oa.sync_results(years=a.years or None, all_years=a.all,
                                              force=a.force))
    if not a.results_only:
        print("планы:", await oa.sync_plans(force=a.force or a.all))
    _report()


if __name__ == "__main__":
    asyncio.run(main())
