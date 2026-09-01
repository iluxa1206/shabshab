#!/usr/bin/env python3
"""Нормализация записи рейтингов в реестре: «A+|ru|» → «A+».

Зачем: источники пишут одно и то же значение четырьмя способами (ruAA-,
AA-(RU), AA-|ru|, AA-.ru). Витрины группируют по ГРЕЙДУ, и запись с суффиксом
агентства раньше не ложилась ни в один чип.

WITHDRAWN («рейтинг отозван») НЕ трогаем: витрины считают его бакетом NR, а сам
факт отзыва — сигнал, который стоит видеть. Снести его в пусто можно флагом
--drop-withdrawn, но по умолчанию скрипт правит только формат записи.

Сухой прогон по умолчанию; --apply пишет.
"""
import re
import sqlite3
import sys

sys.path.insert(0, ".")
from services.instruments_registry import DB_PATH

APPLY = "--apply" in sys.argv
DROP_WITHDRAWN = "--drop-withdrawn" in sys.argv
DB = str(DB_PATH)

TRIM = re.compile(r"\|RU\||\(RU\)|\.RU$|^RU", re.I)
SCALE = re.compile(r"^(AAA|AA|A|BBB|BB|B|CCC|CC|C|D)([+-])?$")


def norm(raw):
    """→ (значение шкалы | None). None = «рейтинга нет» (Withdrawn/мусор)."""
    t = TRIM.sub("", (raw or "").strip()).strip().upper()
    return t if SCALE.match(t) else None


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT isin, short_name, rating FROM instruments "
                     "WHERE rating IS NOT NULL AND rating <> ''").fetchall()
    fixes, drops = [], []
    for r in rows:
        n = norm(r["rating"])
        if n == r["rating"]:
            continue
        if n:
            fixes.append((r["isin"], r["short_name"], r["rating"], n))
        elif DROP_WITHDRAWN:
            drops.append((r["isin"], r["short_name"], r["rating"], None))
    for isin, name, old, new in fixes:
        print(f"{isin} {(name or '')[:14]:14} {old!r} → {new!r}")
    for isin, name, old, _ in drops:
        print(f"{isin} {(name or '')[:14]:14} {old!r} → пусто (рейтинга нет)")
    print(f"\nк нормализации: {len(fixes)}, к очистке: {len(drops)}"
          + ("" if DROP_WITHDRAWN else " (WITHDRAWN не трогаем, см. --drop-withdrawn)"))
    if not APPLY:
        print("сухой прогон; --apply чтобы записать")
        return
    with c:
        for isin, _, _, new in fixes:
            c.execute("UPDATE instruments SET rating=? WHERE isin=?", (new, isin))
        for isin, _, _, _ in drops:
            c.execute("UPDATE instruments SET rating=NULL WHERE isin=?", (isin,))
    print(f"записано: {len(fixes) + len(drops)}")


if __name__ == "__main__":
    main()
