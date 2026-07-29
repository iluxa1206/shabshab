"""Разморозка производных полей спеки фиксинга в реестре инструментов.

ЗАЧЕМ. Round-trip экспорт→импорт xlsx в Справочнике зовёт set_manual(lock=True) и
замораживает ТОГДАШНИЙ вывод парсера проспекта в coupon_mode/fixing_lag/
fixing_lag_unit. Эти поля читаются в ref_data.coupon_formula ПЕРЕД парсером, из-за
чего последующие правки парсера на такие бумаги не действуют (в проде так осело 544
строки — у РЖД/Росагролизинга лежал point/22, снятая midpoint-аппроксимация окна).

ЧТО ДЕЛАЕТ. Обнуляет три производных поля там, где парсер проспекта САМ даёт режим —
дальше спека считается живьём, и будущие правки парсера применяются без миграций.
Строки, где парсер молчит, не трогает: иначе спека потеряется и бумага уйдёт на
эмпирический калибратор. manual_locked НЕ снимает — он же не даёт sync'у
перезаписать обнулённые поля (upsert пропускает _MANUAL_FIELDS у locked-строк).

БЕЗОПАСНОСТЬ. По умолчанию (APPLY=1) снимаются ТОЛЬКО строки, где значение в БД
СОВПАДАЕТ с текущим выводом парсера — замороженный снапшот, разморозка ничего не
меняет в прайсинге сейчас, но открывает будущие фиксы парсера. Строки, где БД ≠
парсер, могут быть ОСОЗНАННОЙ ручной перебивкой (парсер на бумаге врёт) — их
скрипт печатает списком и снимает только с APPLY_CHANGED=1 (просмотри список!).

ЗАПУСК (в контейнере): dry-run по умолчанию, APPLY=1 — запись + бэкап рядом с БД.
    docker compose -f docker-compose.prod.yml exec floaters \
        python scripts/unfreeze_fixing_spec.py
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 floaters \
        python scripts/unfreeze_fixing_spec.py
    # + спорные (БД ≠ парсер):
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 -e APPLY_CHANGED=1 \
        floaters python scripts/unfreeze_fixing_spec.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.coupon_calib import parse_prospectus_formula  # noqa: E402
from services.paths import CACHE_DIR  # noqa: E402

DB_PATH = os.environ.get("INSTRUMENTS_DB") or os.path.join(
    os.path.dirname(CACHE_DIR), "instruments.db")


def main() -> int:
    apply = os.environ.get("APPLY") == "1"
    if not os.path.exists(DB_PATH):
        print(f"БД не найдена: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT isin, short_name, coupon_mode, fixing_lag, fixing_lag_unit, coupon_text
           FROM instruments
           WHERE coupon_mode IS NOT NULL OR fixing_lag IS NOT NULL"""
    ).fetchall()

    apply_changed = os.environ.get("APPLY_CHANGED") == "1"
    match, keep, changed = [], [], []
    for r in rows:
        spec = parse_prospectus_formula(r["coupon_text"] or "") or {}
        mode, lag = spec.get("mode"), spec.get("lag")
        if not mode:
            keep.append(r)
            continue
        if mode != r["coupon_mode"] or (r["fixing_lag"] is not None and lag != r["fixing_lag"]):
            changed.append((r, mode, lag))
        else:
            match.append(r)

    clear = match + ([r for r, _, _ in changed] if apply_changed else [])

    print(f"БД: {DB_PATH}")
    print(f"строк со спекой в БД               : {len(rows)}")
    print(f"  БД == парсер (снимаем, безопасно): {len(match)}")
    print(f"  БД != парсер (спорные{', СНИМАЕМ' if apply_changed else ', оставляем — APPLY_CHANGED=1 чтобы снять'}): {len(changed)}")
    print(f"  парсер молчит (оставляем)        : {len(keep)}")
    for r, mode, lag in changed:
        print(f"    СПОРНАЯ {r['isin']} {str(r['short_name'])[:18]:20} "
              f"БД {str(r['coupon_mode']):8}/{r['fixing_lag']} vs парсер {mode}/{lag}")
    for r in keep:
        print(f"    ОСТАВЛЕНА {r['isin']} {str(r['short_name'])[:18]:20} "
              f"{r['coupon_mode']}/{r['fixing_lag']} (парсер молчит)")

    if not apply:
        print("\n[DRY-RUN] записи не было. APPLY=1 — применить.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{DB_PATH}.bak-{stamp}"
    shutil.copy2(DB_PATH, backup)
    print(f"\nбэкап: {backup} ({os.path.getsize(backup)} байт)")

    conn.executemany(
        """UPDATE instruments
           SET coupon_mode=NULL, fixing_lag=NULL, fixing_lag_unit=NULL
           WHERE isin=?""",
        [(r["isin"],) for r in clear],
    )
    conn.commit()
    left = conn.execute(
        "SELECT count(*) n FROM instruments WHERE coupon_mode IS NOT NULL").fetchone()["n"]
    print(f"снято строк: {len(clear)}; осталось со спекой в БД: {left} (ожидаем {len(keep)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
