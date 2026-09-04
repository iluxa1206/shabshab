#!/usr/bin/env python3
"""Глубокий бэкфилл ДНЕВНЫХ агрегатов торгов: bond_day (безадресные) и
block_day (РПС/адресные).

Зачем: поштучной ленты глубже ~30 дней нет нигде (Alor отдаёт месяц, ISS
trades.json — только текущую сессию), а ISS history хранит ДНЕВНЫЕ итоги за
годы. Слои «покупки/продажи» и «крупные сделки» назад не продлить — стороны
живут только в тике; обороты и адресный объём по дням — можно.

Штатный фоновый воркер (api/main.py) закрывает лишь короткое окно (30 дней) на
каждом такте. Этот скрипт идёт назад на годы и переживает перезапуск: курсор
последней обработанной даты по каждому рынку лежит в data/backfill_days.json,
поэтому повтор запуска продолжает с места остановки, а не с сегодняшнего дня.

    python scripts/backfill_market_days.py --years 10
    python scripts/backfill_market_days.py --years 10 --markets bonds
    python scripts/backfill_market_days.py --reset          # начать заново

Стоимость: одна дата рынка bonds — ~32 страницы ISS (по 100 строк), ndm — 2-3.
Десять лет ≈ 2500 торговых дат, порядка 7 часов сетевой работы. Прервать можно
в любой момент: незавершённая дата будет перезапрошена, дубликатов нет
(UPSERT по ключу isin+date+board).

Выходные пропускаются: облигационные торги MOEX идут только в будни, а запрос
на субботу — пустая страница и потраченный такт.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, timedelta

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import block_trades as bt  # noqa: E402
from services.portfolio_db import DB_PATH  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_days")
# httpx пишет строку на КАЖДЫЙ запрос: за десять лет это ~80 тысяч строк лога
# ради данных, которые и так видны в курсоре и сводках прогресса
logging.getLogger("httpx").setLevel(logging.WARNING)

# курсор кладём рядом с базой: data/ — том, переживающий пересборку контейнера
CURSOR_FILE = str(DB_PATH.parent / "backfill_days.json")
MARKETS = ("bonds", "ndm")


def _load_cursor() -> dict:
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cursor(cur: dict) -> None:
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CURSOR_FILE)


def _earliest(table: str) -> str | None:
    """Самая ранняя уже собранная дата таблицы — точка старта первого прогона:
    ниже неё истории нет, выше её собирает штатный воркер."""
    with bt._connect() as c:  # noqa: SLF001 — свой модуль, отдельного геттера нет
        row = c.execute(f"SELECT MIN(date) FROM {table}").fetchone()
    return row[0] if row and row[0] else None


async def run_market(market: str, years: float, client: httpx.AsyncClient,
                     cursor: dict) -> dict:
    table = "bond_day" if market == "bonds" else "block_day"
    fetch = bt.backfill_bond_day if market == "bonds" else bt.backfill_day
    start = cursor.get(market) or _earliest(table) or date.today().isoformat()
    d = date.fromisoformat(start) - timedelta(days=1)
    limit = date.today() - timedelta(days=int(years * 365.25))
    logger.info("%s: идём с %s назад до %s", market, d.isoformat(), limit.isoformat())

    rows, dates, fails, t0 = 0, 0, 0, time.monotonic()
    while d >= limit:
        if d.weekday() >= 5:            # сб/вс — торгов по облигациям нет
            cursor[market] = d.isoformat()
            _save_cursor(cursor)
            d -= timedelta(days=1)
            continue
        try:
            n = await fetch(d.isoformat(), client)
        except Exception as e:          # сеть/ISS моргнули или база занята
            fails += 1
            logger.warning("%s %s: %s (попытка %d)", market, d, e, fails)
            if fails < 5:
                await asyncio.sleep(5)
                continue
            # пять раз подряд — дальше залипать нельзя: дату пропускаем, курсор
            # двигаем. Повторный прогон с --reset вернётся к ней.
            logger.error("%s %s: пропускаю дату", market, d)
            fails = 0
            cursor[market] = d.isoformat()
            _save_cursor(cursor)
            d -= timedelta(days=1)
            continue
        fails = 0
        rows += n
        dates += 1
        cursor[market] = d.isoformat()
        _save_cursor(cursor)
        if dates % 20 == 0:
            speed = dates / max(time.monotonic() - t0, 1e-9) * 3600
            logger.info("%s: %s, дат %d, строк %d, ~%.0f дат/час",
                        market, d.isoformat(), dates, rows, speed)
        d -= timedelta(days=1)
    logger.info("%s: готово — дат %d, строк %d", market, dates, rows)
    return {"market": market, "dates": dates, "rows": rows}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=10.0,
                    help="глубина в годах назад от сегодня (по умолчанию 10)")
    ap.add_argument("--markets", default="both", choices=("both", "bonds", "ndm"))
    ap.add_argument("--reset", action="store_true",
                    help="сбросить курсор и начать с края уже собранных данных")
    args = ap.parse_args()

    cursor = {} if args.reset else _load_cursor()
    markets = MARKETS if args.markets == "both" else (args.markets,)
    async with httpx.AsyncClient() as client:
        await bt.secid_map(client)      # прогреваем суточные справочники один раз
        await bt.board_ccy_map(client)
        # рынки идут параллельно: ndm лёгкий (2-3 страницы на дату) и закончит
        # задолго до bonds, а общий семафор MOEX всё равно держит темп запросов
        for res in await asyncio.gather(
                *(run_market(m, args.years, client, cursor) for m in markets)):
            logger.info("итог %s", res)


if __name__ == "__main__":
    asyncio.run(main())
