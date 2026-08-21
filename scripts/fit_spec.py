#!/usr/bin/env python
"""Подбор спеки фиксинга (лаг × окно) по ФАКТУ выплат для расходящихся бумаг.

Тонкая обёртка над services.spec_autofit: та же логика, что крутится в ночном
синке, — чтобы ручной прогон и автоматика не разъезжались.

Grid search: для каждой бумаги перебирает lag 0..45 и окно {1, 7, 30, 31, 91,
182, период}, ищет минимальную МЕДИАННУЮ |ошибку| пересчёта прошлых купонов.

Применяет (--apply) ТОЛЬКО если фит сходится и подтверждается на отложенных
купонах — иначе печатает и оставляет как есть: систематика, которую не лечит ни
один лаг, значит другая причина (смещённая маржа, капитализация индекса, битые
данные), и ручная спека её только замаскирует.

Использование:
    .venv/bin/python scripts/fit_spec.py                 # dry-run по всем WARN/BAD
    .venv/bin/python scripts/fit_spec.py --apply
    .venv/bin/python scripts/fit_spec.py ISIN1 ISIN2 --apply
"""
import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.spec_autofit import fit_one, margin_hint, verdict  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("isins", nargs="*", help="ISIN (по умолчанию — все WARN/BAD)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from services import instruments_registry as reg
    if args.isins:
        targets = [(i, reg.get(i) or {}) for i in args.isins]
    else:
        targets = [(r["isin"], reg.get(r["isin"]) or {}) for r in reg.list_spec_mismatch()]
    print(f"кандидатов: {len(targets)}\n")

    today = date.today()
    applied = skipped = 0
    for isin, row in targets:
        try:
            r = await fit_one(isin, row, today)
        except Exception as e:
            print(f"{isin}: ошибка {type(e).__name__}: {e}")
            continue
        if r is None:
            print(f"{isin} {row.get('short_name','')}: недостаточно данных для подбора")
            continue
        w = lambda x: f"окно{x}" if x else "окно=период"
        good, why = verdict(r)
        print(f"{isin} {str(r['name'] or '')[:12]:12s} [{r['base']:7s}] n={r['n']} "
              f"период={r['period']}: текущая лаг{r['cur_lag']}·{w(r['cur_window'])} "
              f"err {r['cur_err']} → лаг{r['fit_lag']}·{w(r['fit_window'])} "
              f"err {r['fit_err']} (train {r['train_err']}, hold-out {r['test_err']}) — {why}")
        hint = margin_hint(r) if not good else None
        if hint is not None:
            print(f"      ↳ систематический сдвиг {r['med_signed']:+} пп во ВСЕХ купонах — "
                  f"похоже смещена МАРЖА: {r['margin_bps']} → ~{hint} bps")
        if args.apply and good:
            params = {"coupon_mode": "average", "fixing_lag": r["fit_lag"],
                      "fixing_lag_unit": "cal"}
            if r["fit_window"]:
                params["avg_window_days"] = r["fit_window"]
            reg.set_manual(isin, params, lock=True)
            applied += 1
        else:
            skipped += 1
    print(f"\nприменено: {applied}, оставлено: {skipped}"
          + ("" if args.apply else " (dry-run: --apply для записи)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
