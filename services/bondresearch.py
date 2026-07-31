"""Спека фиксинга флоатеров с bondresearch.ru (index_floaters).

Источник: https://www.bondresearch.ru/boards/pig_floaters_mk.json — позиционный
массив ~490 бумаг; поля: [1]=ISIN, [33]=лаг (дней, календарные), [35]=бенчмарк
(КС/RUONIA/...), [36]=метод расчёта («Cреднее»/«Отсечка»/«Другое»).

Пишется в отдельные колонки реестра br_fixing_lag/br_coupon_mode (провенанс);
приоритет в прайсинге: manual > bondresearch > парсер проспекта > калибратор
(services/ref_data.coupon_formula). manual_locked не трогается — freeze-trap
исключён, ручные правки Справочника всегда выше.

Зовётся из дневного синка (instruments_sync, шаг 7) и вручную из
scripts/import_bondresearch_specs.py (dry-run/--apply).
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

URL = "https://www.bondresearch.ru/boards/pig_floaters_mk.json"

# индексы позиционного массива
I_ISIN, I_LAG, I_BENCH, I_METHOD = 1, 33, 35, 36

# метод расчёта → единая параметризация (point убран из модели: «Отсечка» =
# average с окном 1 день). NB: «Cреднее» на сайте с ЛАТИНСКОЙ C.
_METHOD_MAP = {
    "среднее": {"coupon_mode": "average"},                          # окно = период
    "отсечка": {"coupon_mode": "average", "avg_window_days": 1},    # точечный фиксинг
}
_OUR_BENCH = {"КС", "RUONIA"}

# Sanity-порог: сайт иногда может отдать пустой/куцый JSON — не затирать
# рабочий слой мусором (та же логика, что sync_active_set min_expected).
_MIN_EXPECTED = 200


def parse_rows(rows: list) -> dict:
    """{isin: {"fixing_lag": int, "coupon_mode": str}} — только валидные строки
    с нашими базами (КС/RUONIA); «Другое»/КБД/ROISfix пропускаются."""
    out = {}
    for r in rows or []:
        try:
            isin = (r[I_ISIN] or "").strip()
            bench = (r[I_BENCH] or "").strip()
            method = (r[I_METHOD] or "").strip().lower().replace("c", "с")  # латиница → кириллица
            lag = int(r[I_LAG])
        except (IndexError, TypeError, ValueError):
            continue
        if not isin or bench not in _OUR_BENCH:
            continue
        m = _METHOD_MAP.get(method)
        if m is None:
            continue
        out[isin] = {"fixing_lag": lag, **m}
    return out


def _extract(payload: dict) -> list:
    rows = payload.get("demo")
    if rows is None and payload:
        rows = next(iter(payload.values()))
    return rows or []


def fetch_specs_sync(url: str = URL) -> dict:
    """Синхронный фетч+парс (CLI-скрипт)."""
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    return parse_rows(_extract(r.json()))


async def fetch_specs(url: str = URL) -> dict:
    """Async фетч+парс (дневной синк)."""
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=30)
        r.raise_for_status()
    return parse_rows(_extract(r.json()))


def apply_specs(specs: dict) -> dict:
    """Записать спеки в br-слой реестра (пересечение с известными ISIN).
    Возвращает статистику {fetched, matched, written}."""
    from services import instruments_registry as reg
    if len(specs) < _MIN_EXPECTED:
        logger.warning("bondresearch: куцый ответ (%d бумаг < %d) — слой не трогаем",
                       len(specs), _MIN_EXPECTED)
        return {"fetched": len(specs), "matched": 0, "written": 0, "skipped": "too small"}
    known = {r["isin"] for r in reg.universe_rows(only_priceable=False, only_floaters=False)}
    hit = {i: s for i, s in specs.items() if i in known}
    written = reg.set_br_specs_bulk(hit)
    return {"fetched": len(specs), "matched": len(hit), "written": written}
