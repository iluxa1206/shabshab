"""Разовый бэкфилл эмитентов (MOEX EMITTER_ID + имя) по реестру инструментов.

ЗАЧЕМ. Фоновый drain в api.main берёт бумаги пачками и уважает окно повтора
(sentinel 0 перепроверяется раз в неделю). Когда нужно догнать сразу — после
фикса, который менял трактовку sentinel'а, или после сетевого сбоя, заклеймившего
пачку бумаг, — этот скрипт проходит ВСЕ нерезолвённые строки без окна.

Синтетический id ОФЗ (отрицательный, ставит normalize_ofz_pk) не трогаем: у ОФЗ
своего EMITTER_ID на ISS нет, перезапрашивать нечего.

Использование:
    python scripts/backfill_emitters.py            # только нерезолвённые
    python scripts/backfill_emitters.py --batch 20 # размер пачки к MOEX
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import instruments_registry as reg          # noqa: E402
from services.market_data import MarketDataService as MD   # noqa: E402


def _pending() -> list[str]:
    """Активные бумаги без эмитента: NULL или sentinel 0 (без окна повтора)."""
    reg._ensure()
    with reg._conn() as c:
        rows = c.execute(
            "SELECT isin FROM instruments WHERE active=1 AND "
            "(emitter_id IS NULL OR emitter_id=0) ORDER BY isin").fetchall()
    return [r["isin"] for r in rows]


async def main(batch: int) -> int:
    todo = _pending()
    print(f"нерезолвённых: {len(todo)}")
    if not todo:
        return 0
    ok = no_field = no_answer = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        emap = await MD.fetch_emitter_info(chunk)
        for isin in chunk:
            got = emap.get(isin)
            if got is None:
                no_answer += 1
                continue
            eid, name = got
            reg.set_emitter(isin, eid, name)
            if eid:
                ok += 1
                print(f"  {isin} → {eid} {name}")
            else:
                no_field += 1
        await asyncio.sleep(0.5)   # мягкий rate-limit между пачками
    print(f"резолвнуто: {ok} | без EMITTER_ID на ISS: {no_field} | MOEX не ответил: {no_answer}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Бэкфилл emitter_id/emitter_name из MOEX")
    ap.add_argument("--batch", type=int, default=20, help="размер пачки запросов к MOEX")
    asyncio.run(main(ap.parse_args().batch))
