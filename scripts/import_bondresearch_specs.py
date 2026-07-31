#!/usr/bin/env python
"""Импорт спеки фиксинга флоатеров с bondresearch.ru (index_floaters).

Логика (URL, парс позиционного JSON, br_* слой, приоритеты) — в
services/bondresearch.py; тот же код зовёт ежедневный синк (шаг 7
instruments_sync). Скрипт — ручной запуск с dry-run диффом.

По умолчанию dry-run (печатает диффы vs текущая эффективная спека).
Запись: --apply.

Использование:
    .venv/bin/python scripts/import_bondresearch_specs.py           # dry-run
    .venv/bin/python scripts/import_bondresearch_specs.py --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from services import bondresearch
    from services import instruments_registry as reg
    from services.ref_data import coupon_formula

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="записать в реестр (иначе dry-run)")
    ap.add_argument("--url", default=bondresearch.URL)
    args = ap.parse_args()

    specs = bondresearch.fetch_specs_sync(args.url)
    print(f"bondresearch: {len(specs)} бумаг с валидной спекой (КС/RUONIA)")

    known = {r["isin"] for r in reg.universe_rows(only_priceable=False, only_floaters=False)}
    hit = {i: s for i, s in specs.items() if i in known}
    print(f"пересечение с реестром: {len(hit)}")

    diffs, same = [], 0
    for isin, s in sorted(hit.items()):
        cur = coupon_formula(isin)
        if (cur.get("fixing_lag") == s["fixing_lag"]
                and cur.get("coupon_mode") == s.get("coupon_mode")
                and (cur.get("avg_window_days") or None) == (s.get("avg_window_days") or None)):
            same += 1
            continue
        diffs.append((isin, cur.get("fixing_lag"), cur.get("coupon_mode"), cur.get("avg_window_days"),
                      s["fixing_lag"], s.get("coupon_mode"), s.get("avg_window_days")))

    print(f"совпадает с текущей спекой: {same}, расходится: {len(diffs)}")
    for isin, cl, cm, cw, nl, nm, nw in diffs:
        w = lambda x: f"·окно{x}" if x else ""
        print(f"  {isin}: {cm}{w(cw)}·lag{cl} → {nm}{w(nw)}·lag{nl}")

    if not args.apply:
        print("\ndry-run: ничего не записано. Запись: --apply")
        return 0

    st = bondresearch.apply_specs(specs)
    print(f"записано br-спек: {st['written']} (fetched {st['fetched']}, matched {st['matched']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
