"""СНИМОК СЧИТАННЫХ МЕТРИК НА ДИСК — чтобы рестарт не начинался с пустой витрины.

Каждый рестарт контейнера ставил в очередь полного пересчёта ВЕСЬ рынок: 612
бумаг по 200–800 мс на одном ядре — две-три минуты, в течение которых у части
строк нет ни спреда, ни дюрации. Пользователю это видно как «спреды не
грузятся», и за один рабочий день с несколькими деплоями набегает изрядно.

Между тем считать заново нечего: метрики зависят от ЦЕНЫ, ДНЯ и КРИВОЙ, и все
три переживают рестарт. Снимок кладём на диск (data/cache — том, переживающий
редеплой) и поднимаем при старте, если совпали:

  - день расчёта (`calc_date`) — назавтра поток и НКД другие;
  - отпечаток кривых (`services.universe_stream._curves_fp`) — на другой кривой
    та же цена даёт другой спред;
  - версия схемы снимка (`SNAPSHOT_VERSION`) — правка полей строки не должна
    оживать из вчерашнего файла.

Не совпало — файл игнорируем целиком: показать устаревшее число хуже, чем
показать прочерк, который через минуту заполнится.

Сохраняем ТОЛЬКО простые типы (числа, строки, вложенные словари и списки из
них). Строка витрины такая и есть; фильтр защищает от того, что кто-нибудь
положит в неё объект — тогда снимок молча потеряет поле, а не сломает загрузку
или, хуже, не подсунет витрине строку вместо даты.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
_FILE = "metrics_snapshot.json"
# Снимок старше этого не поднимаем даже при совпавшем дне: контейнер мог
# простоять с утра до вечера, и «сегодняшние» числа окажутся утренними.
MAX_AGE_SEC = float(os.getenv("METRICS_SNAPSHOT_MAX_AGE_SEC", "10800"))   # 3 ч

_SIMPLE = (int, float, str, bool)


def _clean(v, depth: int = 0):
    """Значение, которое переживёт JSON без сюрпризов. Всё остальное — None."""
    if v is None or isinstance(v, _SIMPLE):
        return v
    if depth >= 3:
        return None
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                continue
            c = _clean(x, depth + 1)
            if c is not None or x is None:
                out[k] = c
        return out
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in v]
    return None


def _clean_rows(rows: dict) -> dict:
    return {isin: _clean(row) for isin, row in (rows or {}).items()
            if isinstance(isin, str) and isinstance(row, dict)}


def save(*, calc_date: str, curves_fp: str, universe: dict, fixed: dict) -> int:
    """Снимок на диск. Возвращает число сохранённых строк (0 — не писали)."""
    from services.paths import atomic_write_json, cache_path
    uni, fx = _clean_rows(universe), _clean_rows(fixed)
    n = len(uni) + len(fx)
    if not n:
        return 0
    try:
        atomic_write_json(cache_path(_FILE), {
            "v": SNAPSHOT_VERSION, "ts": time.time(),
            "calc_date": str(calc_date), "curves_fp": str(curves_fp),
            "universe": uni, "fixed": fx,
        })
    except Exception as e:               # снимок — удобство, а не обязанность
        logger.warning("снимок метрик не сохранён: %s", e)
        return 0
    return n


def _read() -> Optional[dict]:
    from services.paths import cache_path
    path = cache_path(_FILE)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        logger.warning("снимок метрик не прочитан: %s", e)
        return None
    if not isinstance(d, dict) or d.get("v") != SNAPSHOT_VERSION:
        return None
    return d


def restore_early(market_cache: dict) -> int:
    """РАННИЙ ПОДЪЁМ ФИКСОВ: сразу при старте, до прогрева, в пустую витрину.

    Замер 14.10.2026: после деплоя /api/fixed минуту-две отдавал строки без чисел
    (прогрев фиксов идёт ~85 с — расписания, кривые), хотя снимок на диске был
    моложе двух минут. Поздний подъём (universe_stream._restore_snapshot) ждёт
    контекст дня и отпечаток кривых, поэтому к фиксам почти не доходил.

    Здесь отпечаток кривых НЕ проверяем — берём возраст (MAX_AGE_SEC). Это можно,
    потому что строки кладутся с _calc_ts=0: любой настоящий пересчёт (прогрев,
    движок) перекрывает их целиком, а число на экране, чья цена ушла, фронт
    гасит сам. Заполняем только ПУСТУЮ витрину — живую работу не трогаем.
    Возвращает число поднятых строк."""
    d = _read()
    if not d or time.time() - float(d.get("ts") or 0) > MAX_AGE_SEC:
        return 0
    rows = d.get("fixed")
    if not isinstance(rows, dict) or not rows or market_cache.get("fixed_metrics"):
        return 0
    market_cache["fixed_metrics"] = {i: {**r, "_calc_ts": 0.0} for i, r in rows.items()
                                     if isinstance(r, dict)}
    return len(market_cache["fixed_metrics"])


def load(*, calc_date: str, curves_fp: str) -> Optional[dict]:
    """Снимок с диска, если он про ЭТОТ день и ЭТУ кривую. Иначе None."""
    d = _read()
    if not d:
        return None
    if d.get("calc_date") != str(calc_date) or d.get("curves_fp") != str(curves_fp):
        return None
    if time.time() - float(d.get("ts") or 0) > MAX_AGE_SEC:
        return None
    uni, fx = d.get("universe"), d.get("fixed")
    if not isinstance(uni, dict) or not isinstance(fx, dict):
        return None
    return {"universe": uni, "fixed": fx, "ts": float(d.get("ts") or 0)}
