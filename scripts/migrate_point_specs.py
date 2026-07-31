#!/usr/bin/env python
"""Миграция спеки: point убран из модели → average + avg_window_days=1.

Правит РУЧНЫЕ поля строк реестра, где coupon_mode='point' (семантика купона
не меняется: точечный фиксинг = среднее по окну в 1 день с тем же лагом).
BR-слой мигрируется перезапуском импорта bondresearch (Отсечка теперь пишется
как average + br_avg_window_days=1).

Использование:
    .venv/bin/python scripts/migrate_point_specs.py           # dry-run
    .venv/bin/python scripts/migrate_point_specs.py --apply
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from services import instruments_registry as reg
    c = sqlite3.connect(str(reg.DB_PATH))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT isin, short_name, fixing_lag, avg_window_days FROM instruments "
        "WHERE coupon_mode='point'").fetchall()
    print(f"строк с coupon_mode='point': {len(rows)}")
    for r in rows:
        w = r["avg_window_days"] or 1
        print(f"  {r['isin']} {r['short_name'] or '':20s} point·{r['fixing_lag']} → average·окно{w}")
    if not args.apply:
        print("\ndry-run. Запись: --apply")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    c.execute("UPDATE instruments SET coupon_mode='average', "
              "avg_window_days=COALESCE(avg_window_days, 1), updated_at=? "
              "WHERE coupon_mode='point'", (now,))
    c.commit()
    reg.invalidate_params_cache()
    print(f"мигрировано: {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
