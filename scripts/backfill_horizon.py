"""Бэкфилл ГОРИЗОНТА в истории спредов (spread_daily).

Зачем. Строка архива хранит Y-IDX к тому горизонту, что действовал в её день, но
сам горизонт до 14.08.2026 не сохранялся. Горизонт бумаги меняется во времени
(подтянулась дата колла из corpbonds, цена перешла порог выкупа) — и линия
истории склеивала несопоставимые числа: СибурХ1Р04/05/06 12.08.2026 переключились
с погашения (5,6 г) на колл (0,3 г), медиана рейтинг-бакета обвалилась на 220 б.п.
без движения цены.

Что делает. Для каждой бумаги юниверса берёт строки с horizon IS NULL и
пересчитывает их честным as-of движком НА ИХ ЖЕ ЦЕНЕ (тот же путь, что
ensure_honest_backfill): calc_date = дата строки, кривая/НКД/номинал того дня,
горизонт — по правилу цены на ту дату. Пишет y_idx (к выбранному горизонту),
y_idx_alt (ко второму) и оба ключа горизонта. Строки, где горизонт уже проставлен,
не трогает — идемпотентно.

Запуск (в контейнере прода или локально из корня репо):
    python scripts/backfill_horizon.py [--days 400] [--limit N] [--isin ISIN]
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_horizon")
log.setLevel(logging.INFO)


async def backfill(days: int, limit: int | None, only_isin: str | None) -> None:
    from services import instruments_registry
    from services.backdate import honest_spread_series
    from services.portfolio_db import init_db
    from services.spread_history import rows_without_horizon, update_horizon

    init_db()
    uni = await instruments_registry.fetch_floater_universe()
    isins = [u["isin"] for u in uni]
    if only_isin:
        isins = [i for i in isins if i == only_isin.upper()]
    if limit:
        isins = isins[:limit]

    total = touched = skipped = 0
    for i, isin in enumerate(isins, 1):
        gaps = rows_without_horizon(isin, days=days)
        # считать можно только там, где известна цена строки: спред пересчитываем
        # на ней, а не на close (иначе поедет и цена, и метрика)
        overrides = {r["date"]: r["price_pct"] for r in gaps if r.get("price_pct")}
        if not overrides:
            continue
        try:
            series = await honest_spread_series(isin, days, price_overrides=overrides)
            pts = [p for p in (series.get("points") or []) if p["date"] in overrides]
            n = update_horizon(isin, pts)
            total += n
            touched += 1
            log.info("%s: %d строк без горизонта → обновлено %d", isin, len(overrides), n)
        except Exception as e:
            skipped += 1
            log.warning("%s: %s", isin, e)
        if i % 25 == 0 or i == len(isins):
            log.info("%d/%d бумаг · строк обновлено %d · сбоев %d",
                     i, len(isins), total, skipped)
    log.info("готово: %d строк в %d бумагах, %d сбоев", total, touched, skipped)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Проставить горизонт в истории спредов")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    ap.add_argument("--isin", default=None, help="только одна бумага")
    a = ap.parse_args()
    asyncio.run(backfill(a.days, a.limit, a.isin))
