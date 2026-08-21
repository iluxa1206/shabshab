"""Точечный пересчёт истории спреда по СПИСКУ бумаг (без бампа версии движка).

ЗАЧЕМ. Правка, которая меняет цифру не у всех, а у части универса (например,
база Y-IDX до конца потока — задело только бумаги с обрезкой по оферте), не
стоит бампа HONEST_ENGINE_VERSION: тот заставит пересчитать ВСЕ 600 бумаг, из
которых 580 дадут ровно прежние числа. Здесь мы сносим историю только у
затронутых и считаем её заново.

ЧТО ДЕЛАЕТ. Для каждой бумаги: удаляет honest-строки spread_daily (снимки 'snap'
не трогает — их писал живой движок в свой день), зануляет metrics_ver баров
(их досчитает ближайший прогон backfill_hourly_bars или открытие графика),
затем сразу пересчитывает честную историю.

ЗАПУСК (в контейнере прода или локально из корня репо):
    python scripts/recalc_bonds.py --with-offers --days 400
    python scripts/recalc_bonds.py --isins RU000A105VQ5,RU000A108ZH9
    python scripts/recalc_bonds.py --with-offers --dry-run
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recalc_bonds")
log.setLevel(logging.INFO)


async def _offer_isins() -> list:
    """Бумаги, у которых в расписании есть оферта: только их поток может резаться."""
    from services import instruments_registry as reg
    from services.market_data import MarketDataService
    out = []
    for u in await reg.fetch_floater_universe():
        try:
            full = await MarketDataService.fetch_bond_schedule_full(u["isin"])
        except Exception:
            continue
        if full.get("offers"):
            out.append(u["isin"])
    return out


async def main(a) -> None:
    from services.backdate import ensure_honest_backfill
    from services.portfolio_db import init_db, _connect, _lock

    init_db()
    isins = [s.strip().upper() for s in (a.isins or "").split(",") if s.strip()]
    if a.with_offers:
        isins = sorted(set(isins) | set(await _offer_isins()))
    if not isins:
        log.error("нечего пересчитывать: задайте --isins или --with-offers")
        return
    log.info("бумаг к пересчёту: %d · окно %d дн%s",
             len(isins), a.days, " (DRY-RUN)" if a.dry_run else "")

    if a.dry_run:
        with _connect() as c:
            n = c.execute("SELECT count(*) FROM spread_daily WHERE src='honest' "
                          f"AND isin IN ({','.join('?' * len(isins))})", isins).fetchone()[0]
            b = c.execute("SELECT count(*) FROM bar_hourly WHERE metrics_ver IS NOT NULL "
                          f"AND isin IN ({','.join('?' * len(isins))})", isins).fetchone()[0]
        log.info("снесло бы: %d honest-строк, обнулило бы спред у %d баров", n, b)
        return

    ph = ",".join("?" * len(isins))
    with _lock, _connect() as c:
        dropped = c.execute(
            f"DELETE FROM spread_daily WHERE src='honest' AND isin IN ({ph})", isins).rowcount
        # бары не удаляем — только снимаем штамп версии: пересчёт спреда сделает
        # ближайший проход (backfill_hourly_bars / открытие графика), а цена и
        # оборот в баре остаются, их перекачивать незачем
        bars = c.execute(
            f"UPDATE bar_hourly SET metrics_ver=NULL WHERE isin IN ({ph})", isins).rowcount
        c.execute(f"UPDATE bar_daily SET metrics_ver=NULL WHERE isin IN ({ph})", isins)
    log.info("снесено honest-строк: %d · баров без штампа: %d", dropped, bars)

    rows = fails = 0
    for i, isin in enumerate(isins, 1):
        try:
            rows += await ensure_honest_backfill(isin, a.days)
        except Exception as e:
            fails += 1
            log.warning("%s: %s", isin, e)
        if i % 20 == 0 or i == len(isins):
            log.info("%d/%d · строк %d · сбоев %d", i, len(isins), rows, fails)
    log.info("готово: строк %d · сбоев %d", rows, fails)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Пересчёт истории спреда по списку бумаг")
    ap.add_argument("--isins", default=None, help="через запятую")
    ap.add_argument("--with-offers", action="store_true",
                    help="все бумаги, у которых есть оферта в расписании")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args()))
