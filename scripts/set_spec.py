#!/usr/bin/env python
"""CLI-правка спеки фиксинга бумаги в реестре (ручной слой, lock).

Эквивалент формы Справочника: set_manual только переданных полей.
Использование:
    .venv/bin/python scripts/set_spec.py ISIN [--mode average] [--lag 7]
        [--window 31] [--margin-bps 120] [--apply]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("isin")
    ap.add_argument("--mode", choices=["average", "month_start"])
    ap.add_argument("--base", choices=["KEYRATE", "RUONIA", "FIXED", "EXOTIC"],
                    help="EXOTIC — купон вне линейной модели (ИПЦ/GCurve): уходит из универса")
    ap.add_argument("--lag", type=int)
    ap.add_argument("--lag-unit", choices=["cal", "work"])
    ap.add_argument("--window", type=int, help="avg_window_days")
    ap.add_argument("--compounded", type=int, choices=[0, 1],
                    help="1 — индекс капитализируется внутри периода (Index_end/Index_start)")
    ap.add_argument("--margin-bps", type=int)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from services import instruments_registry as reg
    row = reg.get(args.isin)
    if row is None:
        print(f"{args.isin}: нет в реестре")
        return 1
    params = {}
    if args.base:
        params["base"] = args.base
    if args.mode:
        params["coupon_mode"] = args.mode
    if args.lag is not None:
        params["fixing_lag"] = args.lag
    if args.lag_unit:
        params["fixing_lag_unit"] = args.lag_unit
    if args.window is not None:
        params["avg_window_days"] = args.window
    if args.compounded is not None:
        params["compounded"] = args.compounded
    if args.margin_bps is not None:
        params["margin_bps"] = args.margin_bps
    if not params:
        print("нет полей")
        return 1
    cur = {k: row.get(k) for k in ("coupon_mode", "fixing_lag", "fixing_lag_unit",
                                   "avg_window_days", "margin_bps")}
    print(f"{args.isin} {row.get('short_name')}: {cur} → {params}")
    if not args.apply:
        print("dry-run. Запись: --apply")
        return 0
    reg.set_manual(args.isin, params, lock=True)
    print("записано (manual_locked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
