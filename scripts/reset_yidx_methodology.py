"""Сброс y_idx в истории спредов после смены базы Y-IDX (2026-08-04).

ЗАЧЕМ. Base leg Y-IDX стал единым для всех флоатеров — роллирование RUONIA
(капитализация по рабочим дням, выходные простыми) вместо «свой индекс своей
конвенцией». КС-бумаги сдвигаются заметно (только смена конвенции ≈ +85 bps на
плоских 15%, плюс базис КС↔RUONIA), RUONIA-бумаги — на единицы bps. Строки
spread_daily, посчитанные старой методикой, остались бы в базе навсегда: honest
инвалидируются бампом HONEST_ENGINE_VERSION, а вечерние снапшоты (src='snap')
дропу не подлежат — это факт своего дня. Итог без миграции — ступенька в графике
ровно на дате выката.

ЧТО ДЕЛАЕТ. Обнуляет y_idx у floater-строк (все src). Дальше работает штатный
механизм: ensure_honest_backfill видит строки с y_idx IS NULL и price_pct,
пересчитывает Y-IDX НА ИХ ЖЕ ЦЕНЕ новым движком и проставляет через UPDATE
(upsert_honest). Цену/DM/YTM/z не трогаем — они методикой не задеты.
Пересчёт ленивый, по открытию графика бумаги; до него точка Y-IDX не рисуется
(лучше дыра, чем цифра в старом базисе).

ЗАПУСК (в контейнере): dry-run по умолчанию, APPLY=1 — запись.
    docker compose -f docker-compose.prod.yml exec floaters \
        python scripts/reset_yidx_methodology.py
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 floaters \
        python scripts/reset_yidx_methodology.py
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.portfolio_db import _connect, _lock, DB_PATH  # noqa: E402


def main() -> int:
    apply = os.environ.get("APPLY") == "1"
    with _connect() as c:
        total = c.execute(
            "SELECT COUNT(*) n FROM spread_daily WHERE kind='floater' AND y_idx IS NOT NULL"
        ).fetchone()["n"]
        by_src = c.execute(
            "SELECT src, COUNT(*) n FROM spread_daily "
            "WHERE kind='floater' AND y_idx IS NOT NULL GROUP BY src").fetchall()
        # строки без цены пересчитать нечем — их обнуление просто выкинет точку
        no_price = c.execute(
            "SELECT COUNT(*) n FROM spread_daily WHERE kind='floater' "
            "AND y_idx IS NOT NULL AND price_pct IS NULL").fetchone()["n"]

    print(f"БД: {DB_PATH}")
    print(f"floater-строк с y_idx        : {total}")
    for r in by_src:
        print(f"  src={str(r['src'] or '—'):8}          : {r['n']}")
    print(f"  из них без price_pct       : {no_price} (пересчитать нечем — точка исчезнет)")

    if not apply:
        print("\nDRY-RUN. APPLY=1 — обнулить y_idx (пересчёт лениво, по открытию графика).")
        return 0

    with _lock, _connect() as c:
        cur = c.execute("UPDATE spread_daily SET y_idx=NULL WHERE kind='floater' AND y_idx IS NOT NULL")
        n = cur.rowcount or 0
    print(f"\nОБНУЛЕНО: {n} строк. Пересчёт — ensure_honest_backfill при открытии графика.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
