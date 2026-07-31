#!/usr/bin/env python
"""Снять ручные поля фиксинга (fixing_lag/fixing_lag_unit/coupon_mode) у бумаг,
где они конфликтуют со слоем bondresearch (br_*) — чтобы победил BR.

Решение юзера: лаг bondresearch — авторитет; ручные значения, расходящиеся с ним,
остались от старых правок при клэмпе формы «лаг ≤ 30» и от аппроксимаций.
manual_locked и прочие поля (маржа/даты/кэп и т.д.) НЕ трогаются.

Перед записью печатает каждую строку и сохраняет бэкап значений в JSON рядом
с БД (data/fixing_fields_backup_<ts>.json) — откат возможен вручную.

Использование:
    .venv/bin/python scripts/clear_conflicting_fixing_fields.py           # dry-run
    .venv/bin/python scripts/clear_conflicting_fixing_fields.py --apply
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    args = ap.parse_args()

    from services import instruments_registry as reg
    c = sqlite3.connect(str(reg.DB_PATH))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT isin, short_name, fixing_lag, fixing_lag_unit, coupon_mode,
                  br_fixing_lag, br_coupon_mode, manual_locked
           FROM instruments
           WHERE active=1 AND fixing_lag IS NOT NULL AND br_fixing_lag IS NOT NULL
             AND (fixing_lag != br_fixing_lag
                  OR COALESCE(coupon_mode,'') != COALESCE(br_coupon_mode,''))"""
    ).fetchall()

    print(f"конфликтных бумаг: {len(rows)}")
    for r in rows:
        print(f"  {r['isin']} {r['short_name'] or '':20s} "
              f"БД {r['coupon_mode']}·{r['fixing_lag']}"
              f"{' 🔒' if r['manual_locked'] else ''}  →  BR {r['br_coupon_mode']}·{r['br_fixing_lag']}")

    if not args.apply:
        print("\ndry-run: ничего не записано. Запись: --apply")
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(reg.DB_PATH).parent / f"fixing_fields_backup_{ts}.json"
    backup.write_text(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
    for r in rows:
        c.execute("UPDATE instruments SET fixing_lag=NULL, fixing_lag_unit=NULL, "
                  "coupon_mode=NULL, updated_at=? WHERE isin=?",
                  (datetime.now(timezone.utc).isoformat(), r["isin"]))
    c.commit()
    reg.invalidate_params_cache()
    print(f"\nочищено: {len(rows)}; бэкап: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
