#!/usr/bin/env python3
"""Разовая починка рублёвого объёма в тиковом архиве (trade_tick.value).

Тонкая обёртка над services.trades_archive.repair_values — там же и объяснение,
почему объём расходится с биржевым (амортизация в день события, курс валюты
номинала) и почему источник правды — block_trade.value.

Демон чинит только свежее окно (block_trades_worker, ночной такт); этот скрипт
нужен разово после выкатки фикса — пройти весь архив назад.

    python scripts/fix_tick_values.py --dry-run        # только отчёт
    python scripts/fix_tick_values.py --all            # починить весь архив
    python scripts/fix_tick_values.py --days 40        # окно назад
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.trades_archive import repair_values  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="окно назад в днях")
    ap.add_argument("--all", action="store_true", help="весь архив")
    ap.add_argument("--tol", type=float, default=0.01,
                    help="допуск расхождения, доля (0.01 = 1%%)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    res = repair_values(days=a.days, tol=a.tol, dry_run=a.dry_run,
                        since="0000-00-00" if a.all else None)
    verb = "нашлось" if a.dry_run else "исправлено"
    print(f"{verb}: {res['rows']} сделок за {res['days']} дней архива, "
          f"учтённый оборот вырос на {res['delta']/1e9:.2f} млрд ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
