"""Бэкфилл спреда по ценам бара (y_open/high/low/close_bps) для УЖЕ налитых баров.

Зачем отдельно от backfill_hourly_bars.py: тот перекачивает свечи из MOEX ISS
(девять страниц на бумагу за год — долго и шумно по сети), а здесь цены уже
лежат в bar_hourly, нужен только reprice. Для задачи «дозаполнить новые колонки
на всей истории» это на порядок быстрее.

Заодно пересчитывается y_idx_bps по vwap ТЕМ ЖЕ прогоном модели. Без этого
линия (старое значение, посчитанное моделью на момент налива) и свеча (новые
OHLC, посчитанные сегодня) оказываются в разных уровнях — на RU000A1025B5
2026-08-03 расхождение доходило до 30 б.п.

Запуск из корня репо (или в контейнере прода):
    python scripts/backfill_spread_ohlc.py                  # весь юниверс, вся история
    python scripts/backfill_spread_ohlc.py --days 400
    python scripts/backfill_spread_ohlc.py --isin RU000A1025B5 --limit 5
Идемпотентен: повторный прогон переписывает те же значения.
"""
import argparse
import asyncio
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_spread_ohlc")
log.setLevel(logging.INFO)


async def fill_one(isin: str, kind: str, days: int | None) -> tuple[int, int]:
    """(строк в окне, строк со спредом). Модель бумаги грузится один раз."""
    from services.orderbook_svc import build_metrics_fn
    from services.portfolio_db import DB_PATH

    metrics_fn, _calc_date, _face = await build_metrics_fn(isin, kind)
    key = "g_spread_bps" if kind == "fixed" else "y_idx_bps"

    q = "SELECT ts, open, high, low, close, vwap_pct FROM bar_hourly WHERE isin=?"
    args: list = [isin]
    if days:
        q += " AND ts >= date('now', ?)"
        args.append(f"-{days} day")

    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = con.execute(q, args).fetchall()
        memo: dict[float, dict] = {}

        def y(price):
            if price is None:
                return None
            k = round(float(price), 3)
            if k not in memo:
                try:
                    memo[k] = metrics_fn(k) or {}
                except Exception:
                    memo[k] = {}
            return memo[k].get(key)

        # reprice — чистый CPU, уводим с event loop (как в services/bars.py)
        def _crunch():
            return [(y(o), y(h), y(l), y(c), y(w), isin, ts)
                    for ts, o, h, l, c, w in rows]

        from services.heavy import run_heavy
        upd = await run_heavy(_crunch)
        con.executemany(
            "UPDATE bar_hourly SET y_open_bps=?, y_high_bps=?, y_low_bps=?, "
            "y_close_bps=?, y_idx_bps=? WHERE isin=? AND ts=?", upd)
        con.commit()
        return len(rows), sum(1 for u in upd if u[0] is not None)
    finally:
        con.close()


async def main(a) -> None:
    from services.portfolio_db import DB_PATH, init_db

    init_db()   # создаёт колонки на прод-базе (аддитивная миграция)

    con = sqlite3.connect(DB_PATH, timeout=30)
    if a.isin:
        targets = [(i.strip().upper(), a.kind) for i in a.isin.split(",") if i.strip()]
    else:
        targets = [(r[0], r[1] or "floater") for r in con.execute(
            "SELECT isin, kind FROM bar_hourly GROUP BY isin ORDER BY COUNT(*) DESC")]
    con.close()
    if a.limit:
        targets = targets[:a.limit]

    log.info("бумаг к пересчёту: %d", len(targets))
    # прогресс виден на странице СТАТУС: скрипт живёт отдельным процессом,
    # и без общей таблицы про него из API ничего не узнать
    from services import progress
    progress.start("backfill_spread_ohlc", "Пересчёт спреда по ценам бара",
                   total=len(targets),
                   detail=f"глубина {a.days} дн" if a.days else "вся история")
    # Узкое место — не reprice (пара секунд CPU на бумагу), а сборка модели:
    # реестр, купоны, кривая. Она ждёт ввода-вывода, поэтому бумаги идут
    # параллельно; сам счёт всё равно сериализован через run_heavy.
    sem = asyncio.Semaphore(max(1, a.concurrency))
    state = {"ok": 0, "failed": 0, "rows": 0, "seen": 0}

    async def one(isin: str, kind: str) -> None:
        async with sem:
            try:
                rows, done = await fill_one(isin, kind, a.days)
                state["rows"] += done
                state["ok"] += 1
                if a.verbose:
                    log.info("%s: %d/%d баров", isin, done, rows)
            except Exception as e:
                state["failed"] += 1
                log.warning("%s: %s: %s", isin, type(e).__name__, e)
            state["seen"] += 1
            progress.advance("backfill_spread_ohlc",
                             detail=f"баров {state['rows']}"
                                    + (f" · ошибок {state['failed']}" if state["failed"] else ""))
            if state["seen"] % 25 == 0:
                log.info("[%d/%d] баров со спредом по ценам: %d",
                         state["seen"], len(targets), state["rows"])

    try:
        await asyncio.gather(*(one(i, k) for i, k in targets))
    finally:
        progress.finish("backfill_spread_ohlc",
                        detail=f"бумаг {state['ok']} · ошибок {state['failed']} · баров {state['rows']}")
    log.info("готово: бумаг %d, ошибок %d, баров со спредом по ценам %d",
             state["ok"], state["failed"], state["rows"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Спред по ценам бара для истории bar_hourly")
    ap.add_argument("--days", type=int, default=None, help="только последние N дней (по умолчанию вся история)")
    ap.add_argument("--isin", help="конкретные ISIN через запятую")
    ap.add_argument("--kind", default="floater", help="kind для --isin (floater | fixed)")
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="бумаг параллельно (сборка модели ждёт ввода-вывода)")
    ap.add_argument("-v", "--verbose", action="store_true")
    asyncio.run(main(ap.parse_args()))
