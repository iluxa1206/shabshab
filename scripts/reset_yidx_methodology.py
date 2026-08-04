"""Пересчёт y_idx в истории спредов после смены базы Y-IDX (2026-08-04).

ЗАЧЕМ. Base leg Y-IDX стал единым для всех флоатеров — роллирование RUONIA
(капитализация по рабочим дням, выходные простыми) вместо «свой индекс своей
конвенцией». Строки spread_daily, посчитанные старой методикой, остались бы в
базе навсегда: honest-строки инвалидируются бампом HONEST_ENGINE_VERSION, а
кандл-оценка (src IS NULL, ~100k строк от backfill_yidx_history) и вечерние
снапшоты (src='snap') дропу не подлежат. Итог без миграции — ступенька в графике
ровно на дате выката и разный базис в аналитике по эмитентам.

ЧТО ДЕЛАЕТ. По каждой бумаге поднимает тёплый контекст (load_reprice_ctx) и
пересчитывает y_idx на ХРАНИМОЙ цене строки новым движком → UPDATE. Цену, DM,
YTM, z не трогает: они методикой не задеты. Это та же кандл-оценка, что делает
backfill_yidx_history (сегодняшняя кривая × прошлая цена), поэтому для src IS
NULL пересчёт равноценен, а snap-строки теряют точность as-of за несколько дней
архива — сознательный размен на единый базис во всей серии.

Простого обнуления НЕДОСТАТОЧНО: backfill_yidx_history пишет INSERT OR IGNORE и
существующие строки не чинит, а график аналитики читает только y_idx IS NOT NULL
— серия просто исчезла бы до открытия карточки каждой бумаги.

НЕ ТРОГАЕТ src='honest' — as-of строки пересчитает сам движок (drop_stale_honest
по HONEST_ENGINE_VERSION при открытии графика), причём честно на кривую того дня.

Бумаги, для которых контекст не поднимается (погашены/делистинг/нет в MOEX),
остаются со СТАРЫМ y_idx — их история иначе была бы стёрта целиком; скрипт
печатает их числом и списком.

ЗАПУСК (в контейнере): dry-run по умолчанию, APPLY=1 — запись.
    docker compose -f docker-compose.prod.yml exec floaters \
        python scripts/reset_yidx_methodology.py
    docker compose -f docker-compose.prod.yml exec -e APPLY=1 floaters \
        python scripts/reset_yidx_methodology.py
    # отладка: первые N бумаг (LIMIT) или конкретные (ONLY=ISIN[,ISIN…])
    ... -e APPLY=1 -e ONLY=RU000A109VL8 ...
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reset_yidx")
log.setLevel(logging.INFO)

from services.portfolio_db import _connect, _lock, DB_PATH  # noqa: E402

# строки кандл-оценки и вечерних снапшотов; honest живёт своей инвалидацией
_SRC_FILTER = "(src IS NULL OR src='snap')"


def _plan() -> tuple[dict, dict]:
    """({isin: [(date, price)]}, {src: n}) — что пересчитываем."""
    with _connect() as c:
        rows = c.execute(
            f"SELECT isin, date, price_pct FROM spread_daily "
            f"WHERE kind='floater' AND {_SRC_FILTER} AND price_pct IS NOT NULL "
            f"ORDER BY isin, date").fetchall()
        by_src = {r["src"] or "—": r["n"] for r in c.execute(
            f"SELECT src, COUNT(*) n FROM spread_daily WHERE kind='floater' "
            f"AND {_SRC_FILTER} GROUP BY src").fetchall()}
    plan: dict = {}
    for r in rows:
        plan.setdefault(r["isin"], []).append((r["date"], float(r["price_pct"])))
    return plan, by_src


async def run(apply: bool, limit: int | None, only: set | None) -> int:
    from services.market_data import MarketDataService
    from services.bond_details import load_reprice_ctx, reprice_at_price
    from services.paths import cache_path

    plan, by_src = _plan()
    isins = sorted(plan)
    if only:
        isins = [i for i in isins if i in only]
    if limit:
        isins = isins[:limit]
    n_rows = sum(len(plan[i]) for i in isins)

    print(f"БД: {DB_PATH}")
    print(f"бумаг к пересчёту          : {len(isins)}")
    print(f"строк к пересчёту          : {n_rows}")
    for src, n in sorted(by_src.items()):
        print(f"  src={src:8}              : {n}")
    with _connect() as c:
        honest = c.execute("SELECT COUNT(*) n FROM spread_daily "
                           "WHERE kind='floater' AND src='honest'").fetchone()["n"]
    print(f"  src=honest (НЕ трогаем)  : {honest} — пересчитает as-of движок")

    if not apply:
        print("\nDRY-RUN. APPLY=1 — пересчитать и записать.")
        return 0

    cache = MarketDataService.get_local_bond_cache(cache_path("isins_cache.json"))
    done_rows = done_bonds = 0
    failed: list = []
    for i, isin in enumerate(isins, 1):
        try:
            ctx = await load_reprice_ctx(isin, cache)
        except Exception as e:
            failed.append((isin, f"{type(e).__name__}: {e}"))
            continue
        memo: dict = {}
        upd = []
        for d, px in plan[isin]:
            key = round(px, 4)
            m = memo.get(key)
            if m is None:
                try:
                    m = reprice_at_price(ctx, key) or {}
                except Exception:
                    m = {}
                memo[key] = m
            y = m.get("yield_over_index_bps")
            if y is not None:
                upd.append((y, isin, d))
        if upd:
            with _lock, _connect() as c:
                c.executemany(
                    f"UPDATE spread_daily SET y_idx=? WHERE isin=? AND date=? "
                    f"AND kind='floater' AND {_SRC_FILTER}", upd)
            done_rows += len(upd)
            done_bonds += 1
        else:
            failed.append((isin, "движок не отдал y_idx ни на одну цену"))
        await asyncio.sleep(0.05)          # щадим MOEX ISS
        if i % 25 == 0 or i == len(isins):
            log.info("%d/%d бумаг · строк обновлено %d · не вышло %d",
                     i, len(isins), done_rows, len(failed))

    print(f"\nОБНОВЛЕНО: {done_rows} строк по {done_bonds} бумагам.")
    if failed:
        print(f"НЕ ПЕРЕСЧИТАНЫ (остались со старым y_idx): {len(failed)} бумаг")
        for isin, why in failed[:40]:
            print(f"    {isin}: {why}")
        if len(failed) > 40:
            print(f"    … ещё {len(failed) - 40}")
    return 0


def main() -> int:
    apply = os.environ.get("APPLY") == "1"
    limit = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
    only = {s.strip() for s in os.environ["ONLY"].split(",") if s.strip()} \
        if os.environ.get("ONLY") else None
    return asyncio.run(run(apply, limit, only))


if __name__ == "__main__":
    raise SystemExit(main())
