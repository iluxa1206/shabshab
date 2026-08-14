"""Бэкфилл ГОРИЗОНТА в истории спредов (spread_daily).

Зачем. Строка архива хранит Y-IDX к тому горизонту, что действовал в её день, но
сам горизонт до 14.08.2026 не сохранялся. Горизонт бумаги меняется во времени
(подтянулась дата колла из corpbonds, цена перешла порог выкупа) — и линия
истории склеивала несопоставимые числа: СибурХ1Р04/05/06 12.08.2026 переключились
с погашения (5,6 г) на колл (0,3 г), медиана рейтинг-бакета обвалилась на 220 б.п.
без движения цены.

Что делает. Две дороги, по наличию у бумаги оферты/колла:

* НЕТ ни пут-оферт MOEX, ни колл-дат corpbonds — горизонт у такой бумаги всегда
  погашение, спред всех дней уже сопоставим. Пересчитывать нечего: проставляем
  метку horizon='maturity' одним UPDATE, без сети и солвера (это ~80% юниверса).
* ЕСТЬ — горизонт мог меняться от дня к дню, число в строке несопоставимо с
  соседним. Пересчитываем честным as-of движком НА ИХ ЖЕ ЦЕНЕ (тот же путь, что
  ensure_honest_backfill): calc_date = дата строки, кривая/НКД/номинал того дня,
  горизонт — по правилу цены на ту дату. Пишем y_idx (к выбранному горизонту),
  y_idx_alt (ко второму) и оба ключа горизонта.

Метка нужна ВСЕМ бумагам, а не только офертным: вечерний снимок теперь пишет
горизонт, и строка без него для агрегата несопоставима с сегодняшней — история
такой бумаги просто исчезла бы с графика.

Строки, где горизонт уже проставлен, не трогаются — идемпотентно.

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
    from services.market_data import MarketDataService
    from services.portfolio_db import init_db
    from services.spread_history import rows_without_horizon, update_horizon, mark_horizon

    init_db()
    uni = await instruments_registry.fetch_floater_universe()
    isins = [u["isin"] for u in uni]
    if only_isin:
        isins = [i for i in isins if i == only_isin.upper()]
    if limit:
        isins = isins[:limit]

    total = marked = recalced = skipped = 0
    for i, isin in enumerate(isins, 1):
        gaps = rows_without_horizon(isin, days=days)
        if not gaps:
            continue
        # Есть ли у бумаги вообще второй горизонт? Расписание берётся из дневного
        # кэша (память/диск), колл-даты подмешиваются туда же (_with_call_offers) —
        # сети это не стоит. Нет оферт → горизонт всегда погашение.
        try:
            sched = await MarketDataService.fetch_bond_schedule_full(isin)
            has_offers = bool(sched.get("offers"))
        except Exception as e:
            skipped += 1
            log.warning("%s: расписание не прочиталось (%s)", isin, e)
            continue
        if not has_offers:
            n = mark_horizon(isin, "maturity", days=days)
            total += n
            marked += 1
            continue
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
            recalced += 1
            log.info("%s: оферта есть · %d строк без горизонта → пересчитано %d",
                     isin, len(overrides), n)
        except Exception as e:
            skipped += 1
            log.warning("%s: %s", isin, e)
        if i % 25 == 0 or i == len(isins):
            log.info("%d/%d бумаг · строк %d (пересчёт %d, метка %d) · сбоев %d",
                     i, len(isins), total, recalced, marked, skipped)
    log.info("готово: %d строк · пересчитано бумаг %d · помечено %d · сбоев %d",
             total, recalced, marked, skipped)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Проставить горизонт в истории спредов")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    ap.add_argument("--isin", default=None, help="только одна бумага")
    a = ap.parse_args()
    asyncio.run(backfill(a.days, a.limit, a.isin))
