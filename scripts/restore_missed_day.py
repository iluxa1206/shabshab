#!/usr/bin/env python3
"""Восстановление дня, который сервер не дожил до конца.

ЗАЧЕМ. 19.09.2026 хостер выключил VPS в 16:37 (кончились деньги на балансе),
поднялся он 20.09 в 10:52. Что теряется за такой простой и что из этого
восстановимо:
  • тики Alor — восстановимы (/history отдаёт ~30 дней) — их доливает штатный
    дрейн демона, этот скрипт только вызывает его тем же путём;
  • часовые бары — восстановимы (свечи ISS лежат годами), но штатный демон
    их НЕ долил: «вчера покрыто» решалось по одному бару за день (см.
    bars._tail_days_since_pass — системный фикс). Здесь бары дня перечитываются
    принудительно;
  • дневная свёртка bar_daily — пересобирается из часов;
  • строки spread_daily за день — вечерний снимок 19:00 не состоялся, и
    ничем штатным он не воспроизводится: честный as-of движок опирается на
    дневную историю MOEX, а выходную сессию биржа относит к следующему рабочему
    дню (у MOEX «19.09» нет). Берём цену закрытия дня из bar_daily (она из
    свечей ISS, у которых выходные дни есть) и считаем метрики честным as-of на
    ЭТОЙ цене — ровно так, как scripts/repair_weekend_spreads.py переписывает
    выходные строки, только здесь строка создаётся (src='honest').
    Только флоатеры: для фиксов as-of движка нет — их день остаётся без строки.
  • РПС поштучно (block_trade) — НЕвосстановимы, ISS отдаёт ленту только за
    текущую сессию. Дневные агрегаты биржа публикует за рабочие дни, выходные
    уходят в понедельник.

ЗАПУСК (в контейнере прода или локально из корня репо):
    python scripts/restore_missed_day.py 2026-09-19
    python scripts/restore_missed_day.py 2026-09-19 --skip-bars       # только spread_daily
    python scripts/restore_missed_day.py 2026-09-19 --isin RU000A1038V6 --dry-run
Идемпотентен: бары перезаписываются теми же значениями, строки spread_daily
вставляются только там, где их нет.
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("restore_day")
log.setLevel(logging.INFO)


async def refill_bars(day: date, targets: list, concurrency: int, with_ticks: bool) -> dict:
    """Перечитать часы дня по всем бумагам (окно от дня до сегодня) + тики + свёртка."""
    from services import bars as bars_svc
    from services import trades_archive as ta

    days = (date.today() - day).days
    sem = asyncio.Semaphore(concurrency)
    stat = {"papers": len(targets), "bars": 0, "ticks": 0, "daily": 0, "failed": 0}
    done = 0

    async def one(isin: str, kind: str):
        nonlocal done
        async with sem:
            try:
                bars = await bars_svc.build_bars(isin, days, kind)
                stat["bars"] += await asyncio.to_thread(bars_svc.upsert_bars, bars)
                if with_ticks:
                    stat["ticks"] += await ta.drain(isin, days=min(days, ta.ALOR_HISTORY_DAYS))
                    await asyncio.to_thread(ta.enrich_bars_with_ticks, isin,
                                            frm=day.isoformat())
                stat["daily"] += await asyncio.to_thread(bars_svc.build_daily, isin, days)
            except Exception as e:
                stat["failed"] += 1
                log.warning("bars %s: %s", isin, e)
            done += 1
            if done % 50 == 0:
                log.info("бары %d/%d · строк %d · тиков %d · дней %d · ошибок %d",
                         done, len(targets), stat["bars"], stat["ticks"],
                         stat["daily"], stat["failed"])

    await asyncio.gather(*(one(i, k) for i, k in targets))
    return stat


def _missing_floaters(day: date, only_isin: str, rewrite: bool = False) -> list:
    """[(isin, close_pct)] — флоатеры с ценой дня в bar_daily, но без строки
    spread_daily. С rewrite — все восстановленные ранее (src='honest') тоже."""
    from services.portfolio_db import _connect
    q = ("SELECT b.isin, b.close_pct, b.wap_pct FROM bar_daily b "
         "LEFT JOIN spread_daily s ON s.isin=b.isin AND s.date=b.date AND s.kind='floater' "
         "WHERE b.date=? AND b.kind='floater' AND "
         + ("(s.isin IS NULL OR s.src='honest')" if rewrite else "s.isin IS NULL"))
    with _connect() as c:
        rows = c.execute(q, (day.isoformat(),)).fetchall()
    out = []
    for r in rows:
        if only_isin and r["isin"] != only_isin:
            continue
        px = r["close_pct"] if r["close_pct"] is not None else r["wap_pct"]
        if px is not None:
            out.append((r["isin"], float(px)))
    return out


async def restore_spread_row(isin: str, day: date, px: float, dry: bool,
                             rewrite: bool = False) -> bool:
    from services.backdate import (load_backdate_ctx, reprice_asof, _alt_horizon,
                                   HONEST_ENGINE_VERSION)
    from services.valuation import pick_horizon
    from services.portfolio_db import _connect, _lock

    ctx = await load_backdate_ctx(isin, day)
    m = reprice_asof(ctx, px)
    # Верхнеуровневые поля ответа движка — всегда к ПОГАШЕНИЮ; метрика к
    # горизонту правила цены лежит в m["horizons"] и достаётся pick_horizon
    # (как в backdate.asof_bar_metrics). Первый прогон 19.09 брал верхний
    # уровень и подписывал его «put»: у бумаг с офертой (БалтЛиз П21/П14)
    # точка дня оказалась спредом к погашению — обвал 3000→200 без движения цены.
    hz_key = m.get("preferred_horizon") or "maturity"
    h = pick_horizon(m, hz_key)
    y = h.get("yield_over_index_bps", m.get("yield_over_index_bps"))
    if y is None:
        log.debug("%s %s: y_idx не посчитался — строку не создаём", isin, day)
        return False
    hz = h.get("horizon") or hz_key
    alt_key = _alt_horizon(hz, m.get("horizons") or {})
    alt = pick_horizon(m, alt_key) if alt_key else {}
    if dry:
        return True
    with _lock, _connect() as c:
        if rewrite:
            c.execute("DELETE FROM spread_daily WHERE isin=? AND date=? AND kind='floater'",
                      (isin, day.isoformat()))
        c.execute(
            "INSERT OR IGNORE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
            "g_spread_bps,z_bps,ytm,y_idx,src,engine_ver,horizon,y_idx_alt,alt_horizon) "
            "VALUES(?,?,'floater',?,?,NULL,?,?,?,'honest',?,?,?,?)",
            (isin, day.isoformat(), px, h.get("disc_margin_bps", m.get("disc_margin_bps")),
             h.get("sm_bps", m.get("sm_bps")), h.get("yield_xirr_pct", m.get("yield_xirr_pct")),
             y, HONEST_ENGINE_VERSION, hz,
             alt.get("yield_over_index_bps"), alt.get("horizon") if alt else None))
    return True


async def main(a) -> None:
    from services.portfolio_db import init_db
    from services import bars as bars_svc

    init_db()
    day = date.fromisoformat(a.day)
    if day >= date.today():
        sys.exit("день должен быть в прошлом: сегодняшний пишет живой снимок 19:00")

    if not a.skip_bars:
        targets = await bars_svc.universe_targets(("floater", "fixed"))
        if a.isin:
            targets = [(i, k) for i, k in targets if i == a.isin]
        log.info("бары %s: бумаг %d, окно %d дн", day, len(targets), (date.today() - day).days)
        stat = await refill_bars(day, targets, a.concurrency, not a.no_ticks)
        log.info("бары готовы: %s", stat)

    missing = _missing_floaters(day, a.isin, a.rewrite)
    log.info("spread_daily %s: флоатеров без строки, но с ценой дня — %d", day, len(missing))
    sem = asyncio.Semaphore(a.concurrency)
    stat = {"rows": 0, "skipped": 0, "failed": 0}

    async def one(isin: str, px: float):
        async with sem:
            try:
                ok = await restore_spread_row(isin, day, px, a.dry_run, a.rewrite)
            except Exception as e:
                stat["failed"] += 1
                log.warning("%s: %s", isin, e)
                return
            stat["rows" if ok else "skipped"] += 1
            done = sum(stat.values())
            if done % 50 == 0:
                log.info("spread %d/%d · строк %d", done, len(missing), stat["rows"])

    await asyncio.gather(*(one(i, p) for i, p in missing))
    log.info("итого spread_daily: строк %d · без y_idx %d · сбоев %d%s",
             stat["rows"], stat["skipped"], stat["failed"], " [dry]" if a.dry_run else "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Восстановить бары и spread_daily за день простоя")
    ap.add_argument("day", help="YYYY-MM-DD")
    ap.add_argument("--isin", default=None)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--skip-bars", action="store_true", help="не перечитывать бары/тики")
    ap.add_argument("--no-ticks", action="store_true", help="бары без дрейна тиков")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rewrite", action="store_true",
                    help="перезаписать уже существующие строки spread_daily за день")
    a = ap.parse_args()
    a.isin = (a.isin or "").upper() or None
    asyncio.run(main(a))
