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
_NEG_TTL = 86400          # промах corpbonds (404/ошибка) — ретрай через день, не каждый
                          # цикл: иначе драйн вечно упирается в те же битые бумаги
                          # в начале списка и не доходит до остальных
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

    def _fresh(entry) -> bool:
        # запись свежа (пропускаем): рейтинг — _TTL, промах (miss) — короткий _NEG_TTL
        if not entry:
            return False
        ttl = _NEG_TTL if entry.get("miss") else _TTL
        return now - entry.get("ts", 0) <= ttl

    todo = [i for i in isins if not rated.get(i) and not _fresh(cache.get(i))][:cap]
    if not todo:
        return 0
    n = 0
    misses = 0
    # дрейн идёт порциями по cap за цикл поллера — на странице СТАТУС видно,
    # что рейтинги прямо сейчас доезжают, и сколько бумаг осталось в очереди
    from services import progress
    left = sum(1 for i in isins if not rated.get(i) and not _fresh(cache.get(i)))
    progress.start("ratings_drain", "Дозагрузка рейтингов (corpbonds/smart-lab)",
                   total=len(todo), detail=f"в очереди всего {left}")
    async with httpx.AsyncClient(headers=_UA, timeout=15) as client:
        for idx, isin in enumerate(todo):
            progress.set_done("ratings_drain", idx,
                              detail=f"{isin} · найдено {n} · промахов {misses}")
            try:
                r = await fetch_corpbonds(isin, client=client)
            except Exception:
                r = None
            await asyncio.sleep(delay)

            raw = r.get("rating_raw") if r else None
            bucket = rating_to_bucket(raw) if r else "NR"

            # Фолбэк на smart-lab: corpbonds не покрывает свежие/мелкие выпуски
            # (ВДО 2025) → большинство фиксов были NR. smart-lab отдаёт бакет.
            if bucket == "NR":
                try:
                    from services.enrich_smartlab import fetch_smartlab_rating
                    sl = await fetch_smartlab_rating(isin, client=client)
                except Exception:
                    sl = None
                await asyncio.sleep(delay)
                if sl:
                    raw, bucket = f"smartlab:{sl}", rating_to_bucket(sl)

            if bucket == "NR":
                # промах обоих источников — negative-кэш с коротким TTL, чтобы
                # драйн прошёл дальше, а не застревал на битых бумагах.
                cache[isin] = {"raw": raw, "bucket": None, "ts": now, "miss": True}
                misses += 1
                continue
            cache[isin] = {"raw": raw, "bucket": bucket, "ts": now}
            n += 1
            # durable-персист: json-кэш (переживает рестарт через _save) — основной
            # для фиксов; в реестр дублируем, если бумага там есть (флоатеры).
            if reg is not None and bucket != "NR":
                try:
                    if reg.get(isin):
                        reg.set_rating(isin, bucket)
                except Exception:
                    pass
    _save()
    progress.finish("ratings_drain", detail=f"найдено {n} · промахов {misses} из {len(todo)}")
    logger.info("ratings refresh: +%d rated, %d miss (todo %d)", n, misses, len(todo))
    return n
