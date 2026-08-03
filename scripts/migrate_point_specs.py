#!/usr/bin/env python
"""Миграция спеки: point и avg_prev убраны из модели → average + окно.

point    → average + avg_window_days=1 (тот же лаг);
avg_prev → average + avg_window_days=купонный период (окно [start−lag−W,
           start−lag) зафиксировано на старте — та же семантика).
Семантика купона не меняется. BR-слой мигрируется перезапуском импорта
bondresearch.

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
    pts = c.execute(
        "SELECT isin, short_name, fixing_lag, avg_window_days FROM instruments "
        "WHERE coupon_mode='point'").fetchall()
    print(f"строк с coupon_mode='point': {len(pts)}")
    for r in pts:
        w = r["avg_window_days"] or 1
        print(f"  {r['isin']} {r['short_name'] or '':20s} point·{r['fixing_lag']} → average·окно{w}")
    aps = c.execute(
        "SELECT isin, short_name, fixing_lag, avg_window_days, coupon_period_days "
        "FROM instruments WHERE coupon_mode='avg_prev'").fetchall()
    ap_ok = [r for r in aps if r["avg_window_days"] or r["coupon_period_days"]]
    ap_skip = [r for r in aps if not (r["avg_window_days"] or r["coupon_period_days"])]
    print(f"строк с coupon_mode='avg_prev': {len(aps)} (мигрируемых {len(ap_ok)}, без периода {len(ap_skip)})")
    for r in ap_ok:
        w = r["avg_window_days"] or r["coupon_period_days"]
        print(f"  {r['isin']} {r['short_name'] or '':20s} avg_prev·{r['fixing_lag']} → average·окно{w}")
    for r in ap_skip:
        print(f"  {r['isin']} {r['short_name'] or '':20s} ПРОПУСК: период неизвестен")
    if not args.apply:
        print("\ndry-run. Запись: --apply")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    c.execute("UPDATE instruments SET coupon_mode='average', "
              "avg_window_days=COALESCE(avg_window_days, 1), updated_at=? "
              "WHERE coupon_mode='point'", (now,))
    c.execute("UPDATE instruments SET coupon_mode='average', "
              "avg_window_days=COALESCE(avg_window_days, coupon_period_days), updated_at=? "
              "WHERE coupon_mode='avg_prev' AND "
              "(avg_window_days IS NOT NULL OR coupon_period_days IS NOT NULL)", (now,))
    c.commit()
    reg.invalidate_params_cache()
    print(f"мигрировано: {len(pts) + len(ap_ok)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
