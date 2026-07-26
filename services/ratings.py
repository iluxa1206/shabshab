"""Кредитные рейтинги с corpbonds.ru — единый источник для флоатеров и фиксов.

corpbonds.ru/bond/{ISIN} отдаёт агрегированный «Кредитный рейтинг» (напр.
«НКР A», «АКРА AA-(RU)»). Тянем раз в день по всем бумагам (drain в поллере,
rate-limit), кэшируем на диск {isin: {raw, bucket, ts}} и пишем бакет в реестр
инструментов (для флоатеров — вместо замороженных НРД-рейтингов).
"""
from __future__ import annotations

import re
import json
import time
import asyncio
import logging
from typing import Optional, Dict, List

from services.paths import cache_path

logger = logging.getLogger(__name__)

_FILE = cache_path("ratings_cache.json")
_TTL = 7 * 86400          # перезапрашиваем не чаще раза в неделю на бумагу
_cache: Optional[dict] = None

# порядок проверки: длинные грейды раньше коротких (BBB до BB до B)
_ORDER = ["AAA", "BBB", "AA", "BB", "A", "B"]
_BUCKETS = {"AAA", "AA", "A", "BBB", "BB", "B"}


def rating_to_bucket(raw: Optional[str]) -> str:
    """«НКР A» / «АКРА AA-(RU)» / «ruBBB+» → бакет AAA…B, иначе NR.
    Кириллицу-агентство и суффиксы (RU/+/−) отбрасываем, берём латинский грейд."""
    if not raw:
        return "NR"
    t = re.sub(r"[^A-D]", "", raw.upper().replace("RU", ""))
    if not t:
        return "NR"
    for b in _ORDER:
        if t.startswith(b):
            return b if b in _BUCKETS else "B"
    if t[0] in ("C", "D"):
        return "B"   # ниже B — распихиваем в самый рисковый бакет
    return "NR"


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_FILE, encoding="utf-8") as f:
                _cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            _cache = {}
    return _cache


def _save() -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except OSError as e:
        logger.warning(f"ratings save failed: {e}")


def bucket_of(isin: str) -> Optional[str]:
    r = _load().get(isin)
    return r.get("bucket") if r else None


def bucket_of_fixed(isin: str, cls: Optional[str]) -> Optional[str]:
    """Рейтинг ОДНОЙ фикс-бумаги (карточка). Для списков — bucket_map_fixed
    (батч, без per-row SQLite). ОФЗ — суверен → AAA; корп — json-кэш, при
    промахе фолбэк на реестр."""
    if cls == "ofz":
        return "AAA"
    b = bucket_of(isin)
    if b:
        return b
    try:
        from services import instruments_registry as reg
        rec = reg.get(isin)
        if rec and rec.get("rating"):
            return rec["rating"]
    except Exception:
        pass
    return None


def bucket_map_fixed(items) -> Dict[str, Optional[str]]:
    """Рейтинги СПИСКА фикс-бумаг батчем — {isin: bucket}. items: iterable of
    (isin, cls). ОФЗ→AAA; корп — json-кэш, остаток ОДНИМ запросом в реестр
    (было: reg.get() per-row = сотни мс на /api/fixed при пустом json-кэше)."""
    cache = _load()
    out: Dict[str, Optional[str]] = {}
    need = []
    for isin, cls in items:
        if cls == "ofz":
            out[isin] = "AAA"
        elif isin in cache and cache[isin].get("bucket"):
            out[isin] = cache[isin]["bucket"]
        else:
            out[isin] = None
            need.append(isin)
    if need:
        try:
            from services import instruments_registry as reg
            rmap = reg.ratings_map(need)
            for i in need:
                if rmap.get(i):
                    out[i] = rmap[i]
        except Exception:
            pass
    return out


def bucket_map(isins: List[str]) -> Dict[str, str]:
    c = _load()
    return {i: c[i]["bucket"] for i in isins if i in c and c[i].get("bucket")}


async def refresh(isins: List[str], cap: int = 80, delay: float = 0.6) -> int:
    """Дотягивает рейтинги для isins, у кого нет/протух (>7 дней), не более cap за
    вызов (drain в поллере). Пишет бакет в реестр. Возвращает число обновлённых."""
    from services.enrich_corpbonds import fetch_corpbonds, _UA
    import httpx
    cache = _load()
    now = time.time()
    try:
        from services import instruments_registry as reg
    except Exception:
        reg = None
    # Дедуп по РЕЕСТРУ (durable): json-кэш живёт в памяти и теряется при рестарте
    # → без этого todo = весь универс каждый рестарт = вечный передрайн corpbonds.
    # Реестр хранит рейтинг постоянно (set_rating), поэтому уже-рейтингованные
    # бумаги пропускаем (ратинги меняются редко; corpbonds часто 404).
    rated = reg.ratings_map(isins) if reg is not None else {}
    todo = [i for i in isins
            if not rated.get(i)
            and (not cache.get(i) or now - cache[i].get("ts", 0) > _TTL)][:cap]
    if not todo:
        return 0
    n = 0
    async with httpx.AsyncClient(headers=_UA, timeout=15) as client:
        for isin in todo:
            try:
                r = await fetch_corpbonds(isin, client=client)
            except Exception:
                r = None
            await asyncio.sleep(delay)
            if r is None:
                continue  # страница не загрузилась → не кэшируем NR, ретрай позже
            raw = r.get("rating_raw")
            bucket = rating_to_bucket(raw)
            cache[isin] = {"raw": raw, "bucket": bucket, "ts": now}
            n += 1
            if reg is not None and bucket != "NR":
                try:
                    if reg.get(isin):
                        reg.set_rating(isin, bucket)
                except Exception:
                    pass
    _save()
    logger.info("ratings refresh: +%d (todo %d)", n, len(todo))
    return n
