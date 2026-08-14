"""Свёртка часовых баров в дневные (bar_daily) по всему юниверсу.

Дневная строка — средневзвешенная цена дня и спред по ней плюс закрытие дня и
спред по закрытию. Это ЧИСТАЯ АГРЕГАЦИЯ уже посчитанных часов: ни сети, ни
солвера, поэтому проход по всему юниверсу занимает секунды, а не часы.

Идемпотентно: день пересобирается, только если его нет, если он посчитан прошлой
версией движка спреда (bars.BARS_METRICS_VERSION) или если в часах прибавилось
оборота (дозалив хвоста, обогащение тиками). --force ломает это правило и
пересобирает всё — нужен только при смене методики свёртки.

Запуск из корня репо (или в контейнере прода):
    python scripts/backfill_daily_bars.py              # вся глубина архива
    python scripts/backfill_daily_bars.py --days 400
    python scripts/backfill_daily_bars.py --limit 20   # отладка
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_daily")
log.setLevel(logging.INFO)
logging.getLogger("services.bars").setLevel(logging.INFO)


async def main(a) -> None:
    from services.portfolio_db import init_db
    from services import bars as bars_svc

    init_db()
    stat = await bars_svc.build_daily_universe(
        days=a.days, limit=a.limit,
        kinds=tuple(k.strip() for k in a.kinds.split(",") if k.strip()),
        force=a.force)
    log.info("готово: %s", stat)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Свёртка bar_hourly → bar_daily")
    ap.add_argument("--days", type=int, default=None, help="окно в днях (по умолчанию вся глубина)")
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    ap.add_argument("--kinds", default="floater,fixed")
    ap.add_argument("--force", action="store_true",
                    help="пересобрать даже готовые дни (смена методики свёртки)")
    asyncio.run(main(ap.parse_args()))
