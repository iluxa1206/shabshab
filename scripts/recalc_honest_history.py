"""Пересчёт ЧЕСТНОЙ as-of истории спредов по всему юниверсу (spread_daily).

ЗАЧЕМ. Строки src='honest' штампуются HONEST_ENGINE_VERSION, и при бампе версии
движок сносит устаревшие сам — но лениво, при первом открытии графика бумаги.
После правки, которая двигает цифру на сотни bps, ждать читателя нельзя: до его
прихода аналитика по эмитентам и бакетам считает медианы по строкам двух разных
движков. Скрипт проходит юниверс и делает то же, что сделал бы читатель, — сразу
и для всех.

ЧТО ДЕЛАЕТ. По каждой бумаге ensure_honest_backfill: строки старой версии
сносятся, даты без строки считаются заново честным as-of (кривая/НКД/номинал/
горизонт того дня). Снапшоты src='snap' не трогаются — их писал ЖИВОЙ движок в
свой день, они версией as-of не помечены и от её правок не зависят.

ЦЕНА. Честный as-of строит контекст на каждый день: порядка минут на бумагу за
годовое окно. Полный проход по ~1300 бумагам идёт часами — это разовая
миграция, а не регулярная работа (регулярную делает ночной прогрев топ-200).
Параллелить агрессивно нельзя: MOEX режет по частоте, а прод-контейнер живёт под
768 MiB — отсюда дефолт concurrency=2.

ЗАПУСК (в контейнере прода или локально из корня репо):
    python scripts/recalc_honest_history.py --days 400
    python scripts/recalc_honest_history.py --days 400 --limit 20      # отладка
    python scripts/recalc_honest_history.py --isin RU000A107DS5
Идемпотентен: строки текущей версии движка повторный прогон не трогает.
"""
import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recalc_honest")
log.setLevel(logging.INFO)


async def main(a) -> None:
    from services import instruments_registry
    from services.backdate import ensure_honest_backfill, HONEST_ENGINE_VERSION
    from services.portfolio_db import init_db

    init_db()
    uni = await instruments_registry.fetch_floater_universe()
    isins = [u["isin"] for u in uni]
    if a.isin:
        isins = [i for i in isins if i == a.isin.upper()]
    if a.limit:
        isins = isins[:a.limit]

    log.info("движок v%d · бумаг %d · окно %d дн · параллельно %d",
             HONEST_ENGINE_VERSION, len(isins), a.days, a.concurrency)

    sem = asyncio.Semaphore(a.concurrency)
    done = {"n": 0, "rows": 0, "fail": 0}
    t0 = time.monotonic()

    async def one(isin: str) -> None:
        async with sem:
            try:
                n = await ensure_honest_backfill(isin, a.days)
                done["rows"] += n
            except Exception as e:
                done["fail"] += 1
                log.warning("%s: %s", isin, e)
            finally:
                done["n"] += 1
                if done["n"] % 25 == 0 or done["n"] == len(isins):
                    el = time.monotonic() - t0
                    left = (el / done["n"]) * (len(isins) - done["n"])
                    log.info("%d/%d бумаг · строк %d · сбоев %d · прошло %.0f мин · "
                             "осталось ~%.0f мин",
                             done["n"], len(isins), done["rows"], done["fail"],
                             el / 60, left / 60)

    await asyncio.gather(*(one(i) for i in isins))
    log.info("готово: строк %d · сбоев %d · за %.0f мин",
             done["rows"], done["fail"], (time.monotonic() - t0) / 60)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Пересчёт честной as-of истории спредов")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    ap.add_argument("--isin", default=None, help="только одна бумага")
    ap.add_argument("--concurrency", type=int, default=2)
    asyncio.run(main(ap.parse_args()))
