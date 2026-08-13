"""Бэкфилл часовых баров (средневзвешенная цена + спред) по всему юниверсу.

Свечи MOEX ISS отдают часы на годы назад — цену/спред можно налить сразу на всю
глубину. Тики (стороны сделок, крупные принты) у брокера живут ~30 дней, глубже
их не существует: --days больше 30 для тиков просто обрежется.

Запуск из корня репо (или в контейнере прода):
    python scripts/backfill_hourly_bars.py --days 365            # цена+спред за год
    python scripts/backfill_hourly_bars.py --days 30             # + тики (окно брокера)
    python scripts/backfill_hourly_bars.py --days 90 --no-ticks --limit 20
Идемпотентен: бары перезаписываются теми же значениями, тики — INSERT OR IGNORE.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_bars")
log.setLevel(logging.INFO)
logging.getLogger("services.bars").setLevel(logging.INFO)


async def main(a) -> None:
    from services.portfolio_db import init_db
    from services import bars as bars_svc

    init_db()
    if a.hot:
        # тот же прогрев, что ночной воркер: топ по обороту на окно, которое
        # реально смотрят. Полный проход по универсу на всю глубину нереален —
        # честный as-of строит кривую/НКД на каждый день (минуты на бумагу).
        stat = await bars_svc.warm_hot(days=a.days, top=a.hot,
                                       concurrency=a.concurrency)
        log.info("готово (hot): %s", stat)
        return
    kinds = tuple(k.strip() for k in a.kinds.split(",") if k.strip())
    stat = await bars_svc.refresh_universe(
        days=a.days, limit=a.limit, with_ticks=not a.no_ticks,
        concurrency=a.concurrency, kinds=kinds, refetch_ticks=a.refetch_ticks)
    log.info("готово: %s", stat)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Бэкфилл часовых баров в bar_hourly")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    ap.add_argument("--no-ticks", action="store_true", help="только свечи, без Alor")
    ap.add_argument("--refetch-ticks", action="store_true",
                    help="игнорировать водяной знак дрейна и перекачать окно сделок "
                         "заново (ремонт дыры; обычный прогон и так тянет всё новое)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--kinds", default="floater,fixed", help="floater,fixed")
    ap.add_argument("--hot", type=int, default=None,
                    help="греть только топ-N бумаг по обороту (как ночной воркер)")
    asyncio.run(main(ap.parse_args()))
