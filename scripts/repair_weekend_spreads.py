"""Перезапись строк ВЫХОДНЫХ ДНЕЙ в spread_daily честными as-of числами.

Зачем. Субботы и воскресенья вечерний снимок писал живой моделью того дня, а
биржевой истории за эти даты не существует вовсе: выходную сессию MOEX относит к
следующему рабочему дню. Пока дата расчётов считалась как T+1 от самой даты
(до фикса core.valuation.settle_date), накануне амортизации окно (calc, settle]
не захватывало выплату: модель дисконтировала полный номинал против цены, уже
котируемой от нового остатка. БалтЛизП10 08–09.08.2026 — Y-IDX 1347/1310 против
805 в соседние будни при неподвижной цене.

Что делает. Для каждой выходной строки берёт ЕЁ ЖЕ цену и пересчитывает метрики
честным as-of движком (своя кривая/НКД/номинал на дату, settle по новому
правилу), после чего переписывает y_idx/dm/z/ytm/горизонты и штампует
src='honest' + engine_ver. Цену не трогает: она пришла с биржи.

Идемпотентно: строки, уже посчитанные текущей версией движка, пропускаются
(--force пересчитывает всё).

Запуск (в контейнере прода или локально из корня репо):
    python scripts/repair_weekend_spreads.py --dry-run
    python scripts/repair_weekend_spreads.py --days 400
    python scripts/repair_weekend_spreads.py --isin RU000A108777
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("repair_weekend")
log.setLevel(logging.INFO)


def _weekend_rows(days: int, only_isin: str, force: bool) -> dict:
    """{isin: [(date, price)]} — выходные строки, которым нужен пересчёт."""
    from services.backdate import HONEST_ENGINE_VERSION
    from services.portfolio_db import _connect
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    q = ("SELECT isin, date, price_pct, src, engine_ver FROM spread_daily "
         "WHERE kind='floater' AND price_pct IS NOT NULL AND date >= ? ORDER BY isin, date")
    with _connect() as c:
        rows = c.execute(q, (cutoff,)).fetchall()
    out: dict = {}
    for r in rows:
        if only_isin and r["isin"] != only_isin:
            continue
        d = date.fromisoformat(r["date"])
        if d.weekday() < 5:
            continue
        # СЕГОДНЯШНЮЮ строку не трогаем: as-of движок работает только по прошлому
        # (для текущего дня есть живая модель), да и снимок дня ещё не финальный
        if d >= date.today():
            continue
        if not force and r["src"] == "honest" and (r["engine_ver"] or 0) >= HONEST_ENGINE_VERSION:
            continue
        out.setdefault(r["isin"], []).append((r["date"], r["price_pct"]))
    return out


async def repair_one(isin: str, items: list, dry: bool) -> int:
    from services.backdate import (load_backdate_ctx, reprice_asof, _alt_horizon,
                                   HONEST_ENGINE_VERSION)
    from services.valuation import pick_horizon
    from services.portfolio_db import _connect, _lock

    upd = []
    for d_iso, px in items:
        d = date.fromisoformat(d_iso)
        try:
            ctx = await load_backdate_ctx(isin, d)
            m = reprice_asof(ctx, px)
        except Exception as e:
            # одна дата не должна ронять всю бумагу: у неликвида в отдельные дни
            # нет ни строки истории, ни кривой
            log.debug("%s %s: %s", isin, d_iso, e)
            continue
        hz = m.get("preferred_horizon") or "maturity"
        alt_key = _alt_horizon(hz, m.get("horizons") or {})
        alt = pick_horizon(m, alt_key) if alt_key else {}
        y = m.get("yield_over_index_bps")
        if y is None:
            log.debug("%s %s: y_idx не посчитался — строку не трогаем", isin, d_iso)
            continue
        upd.append((y, m.get("disc_margin_bps"), m.get("sm_bps"),
                    m.get("yield_xirr_pct"), hz, alt.get("yield_over_index_bps"),
                    alt.get("horizon") if alt else None,
                    HONEST_ENGINE_VERSION, isin, d_iso))
    if not upd or dry:
        return len(upd)
    with _lock, _connect() as c:
        c.executemany(
            "UPDATE spread_daily SET y_idx=?, dm_bps=?, z_bps=?, ytm=?, horizon=?, "
            "y_idx_alt=?, alt_horizon=?, src='honest', engine_ver=? "
            "WHERE isin=? AND date=? AND kind='floater'", upd)
    return len(upd)


async def main(a) -> None:
    from services.portfolio_db import init_db

    init_db()
    targets = _weekend_rows(a.days, (a.isin or "").upper(), a.force)
    total_rows = sum(len(v) for v in targets.values())
    log.info("выходных строк к пересчёту: %d у %d бумаг", total_rows, len(targets))

    sem = asyncio.Semaphore(a.concurrency)
    stat = {"rows": 0, "papers": 0, "failed": 0}

    async def one(isin: str, items: list):
        async with sem:
            try:
                n = await repair_one(isin, items, a.dry_run)
            except Exception as e:
                stat["failed"] += 1
                log.warning("%s: %s", isin, e)
                return
            if n:
                stat["rows"] += n
                stat["papers"] += 1
            done = stat["papers"] + stat["failed"]
            if done % 25 == 0:
                log.info("%d/%d бумаг · строк %d", done, len(targets), stat["rows"])

    await asyncio.gather(*(one(i, v) for i, v in targets.items()))
    log.info("итого: строк %d у %d бумаг · сбоев %d%s",
             stat["rows"], stat["papers"], stat["failed"], " [dry]" if a.dry_run else "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Честный пересчёт выходных строк spread_daily")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--isin", default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args()))
