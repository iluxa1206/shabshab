"""Кредитные рейтинги с corpbonds.ru — единый источник для флоатеров и фиксов.

corpbonds.ru/bond/{ISIN} отдаёт агрегированный «Кредитный рейтинг» (напр.
«НКР A», «АКРА AA-(RU)»). Тянем раз в день по всем бумагам (drain в поллере,
rate-limit), кэшируем на диск {isin: {raw, bucket, ts}} и пишем бакет в реестр
инструментов (для флоатеров — вместо замороженных НРД-рейтингов).
"""
from __future__ import annotations

import re
import json
import datetime as _dt
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

_BUCKETS = {"AAA", "AA", "A", "BBB", "BB", "B"}
# Суффиксы/префиксы агентства: «ruAA-», «AA-(RU)», «AA-|ru|», «AA-.ru» — одно
# значение, записанное четырьмя способами (в выгрузках встречаются все).
_RT_TRIM = re.compile(r"\|RU\||\(RU\)|\.RU$|^RU", re.I)
# Шкала: грейд + необязательная СТУПЕНЬ (+/−). Строгий матч, а не «выкинем
# лишнее»: прежняя реализация чистила всё не-[A-D] и превращала «Withdrawn» в
# «DA» → бакет B, то есть бумага с ОТОЗВАННЫМ рейтингом ехала в самый рисковый
# грейд вместо NR.
_RT_SCALE = re.compile(r"^(AAA|AA|A|BBB|BB|B|CCC|CC|C|D)([+-])?$")
# Кириллица В ЗНАЧЕНИИ рейтинга: «АА-», «А+», «ВВ+» пишутся русскими буквами и
# в выгрузках, и в анонсах первички. Прежняя чистка выбрасывала их как «не
# латиница» — рейтинг превращался в NR, то есть бумага с AA- уезжала к
# безрейтинговым. Транслитерация обязана идти ДО чистки.
_CYR2LAT = str.maketrans("АВСЕDО", "ABCEDO")

# Порядок шкалы СО СТУПЕНЬЮ: индекс = ранг, 0 — лучший. Нужен там, где из
# нескольких оценок берётся ХУДШАЯ (рейтинг бумаги = минимум по агентствам).
_RT_RANK = {r: i for i, r in enumerate((
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"))}


def rating_rank(raw: Optional[str]) -> Optional[int]:
    """Ранг по шкале (0 = AAA, больше = хуже). None — рейтинга нет."""
    return _RT_RANK.get(rating_norm(raw))


def rating_min(*raws) -> str:
    """ХУДШИЙ из переданных рейтингов («минимальный» в разговорной речи).
    Пустые/нераспознанные игнорируются; ничего не осталось — "".

    Именно минимум, а не средний и не первый попавшийся: инвестор ограничен
    самой низкой оценкой, а не самой лестной."""
    best = ""
    worst = -1
    for r in raws:
        rk = rating_rank(r)
        if rk is not None and rk > worst:
            worst, best = rk, rating_norm(r)
    return best


def rating_norm(raw: Optional[str]) -> str:
    """«НКР A» / «АКРА AA-(RU)» / «ruBBB+» → значение шкалы СО СТУПЕНЬЮ («BBB+»).
    Нераспознанное (Withdrawn, мусор, пусто) → "" — это «рейтинга нет»."""
    if not raw:
        return ""
    t = str(raw).strip().upper().translate(_CYR2LAT)
    # ПОТОКЕННО, а не «вычистить всё лишнее»: после транслитерации кириллицы
    # название агентства само похоже на рейтинг — «АКРА» → «AKPA» → чистка
    # оставляла бы «AA». Токен принимается, только если ЦЕЛИКОМ лежит на шкале.
    hits = []
    for tok in re.split(r"[\s()|.,/]+", t):
        tok = _RT_TRIM.sub("", tok)
        if _RT_SCALE.match(tok):
            hits.append(tok)
    if not hits:
        return ""
    # В строке может стоять СРАЗУ ДВЕ оценки («A/BBB», «AA (AA-)»): берём
    # худшую. Инвестор ограничен низшей оценкой, а не той, что написана первой.
    return max(hits, key=lambda r: _RT_RANK.get(r, -1))


def rating_to_bucket(raw: Optional[str]) -> str:
    """Значение шкалы → ГРЕЙД AAA…B (ступень +/− схлопывается), иначе NR.
    Ниже B (CCC/CC/C/D) — тот же глубокий хай-йилд, отдельной корзины нет."""
    core = rating_norm(raw).rstrip("+-")
    if core in _BUCKETS:
        return core
    return "B" if core in ("CCC", "CC", "C", "D") else "NR"


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


def _br():
    """Слой bondresearch — ПРИОРИТЕТНЫЙ источник (разбивка по агентствам + дата
    оценки). Не замена: 214 наших бумаг есть только в corpbonds, поэтому ниже
    везде идёт фолбэк. Импорт ленивый — модуль читает свой кэш при первом
    обращении."""
    from services import ratings_br
    return ratings_br


def bucket_of(isin: str) -> Optional[str]:
    b = _br().ratings_of(isin)
    if b and b.get("bucket"):
        return b["bucket"]
    r = _load().get(isin)
    return r.get("bucket") if r else None


def bucket_of_fixed(isin: str, cls: Optional[str]) -> Optional[str]:
    """Рейтинг ОДНОЙ фикс-бумаги (карточка). Для списков — bucket_map_fixed
    (батч, без per-row SQLite). ОФЗ — суверен → AAA; корп — json-кэш, при
    промахе фолбэк на реестр."""
    if cls == "ofz":
        return "AAA"
    b = bucket_of(isin)          # внутри: bondresearch → corpbonds
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
    items = list(items)
    br = _br().bucket_map([i for i, _c in items])
    out: Dict[str, Optional[str]] = {}
    need = []
    for isin, cls in items:
        if cls == "ofz":
            out[isin] = "AAA"
        elif br.get(isin):
            out[isin] = br[isin]
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
    out = {i: c[i]["bucket"] for i in isins if i in c and c[i].get("bucket")}
    out.update(_br().bucket_map(isins))      # bondresearch перекрывает corpbonds
    return out


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
    # НО отсеиваем ПО ВРЕМЕНИ ПРОВЕРКИ, а не по факту наличия рейтинга: раньше
    # условие было `not rated.get(i)`, и бумага, которой рейтинг однажды
    # записали, исчезала из очереди НАВСЕГДА — понижение AAA→A не доезжало ни
    # через неделю, ни через год, а json-TTL до неё просто не доходил.
    checked = reg.rating_checked_map(isins) if reg is not None else {}
    rated = reg.ratings_map(isins) if reg is not None else {}
    # бумаги, чей рейтинг ведёт приоритетный слой bondresearch: их значение в
    # реестре дрейн не трогает (см. запись ниже)
    try:
        br_have = set(_br().bucket_map(isins))
    except Exception:
        br_have = set()

    def br_covered(isin: str) -> bool:
        return isin in br_have

    def _reg_fresh(isin: str) -> bool:
        """Рейтинг проверялся недавно (durable отметка реестра)."""
        ts = checked.get(isin)
        if not ts:
            # отметки нет — легаси-строка: рейтинг есть, а когда проверяли,
            # неизвестно. Ставим в очередь, но с низким приоритетом (в хвост),
            # чтобы разовый прогон не выгреб весь универс за один цикл.
            return False
        try:
            age = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.datetime.fromisoformat(ts)).total_seconds()
        except (TypeError, ValueError):
            return False
        return age <= _TTL

    def _fresh(entry) -> bool:
        # запись свежа (пропускаем): рейтинг — _TTL, промах (miss) — короткий _NEG_TTL
        if not entry:
            return False
        ttl = _NEG_TTL if entry.get("miss") else _TTL
        return now - entry.get("ts", 0) <= ttl

    # ХВОСТ ОЧЕРЕДИ — легаси-строки с рейтингом, но без отметки времени: их
    # много (разовая миграция), и вперёд должны идти те, у кого рейтинга нет
    # вовсе или отметка реально протухла.
    _stale = [i for i in isins
              if not _reg_fresh(i) and not _fresh(cache.get(i))]
    _head = [i for i in _stale if not rated.get(i) or checked.get(i)]
    _tail = [i for i in _stale if i not in set(_head)]
    todo = (_head + _tail)[:cap]
    if not todo:
        return 0
    n = 0
    misses = 0
    # дрейн идёт порциями по cap за цикл поллера — на странице СТАТУС видно,
    # что рейтинги прямо сейчас доезжают, и сколько бумаг осталось в очереди
    from services import progress
    left = len(_stale)
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
            #
            # НО НЕ ПОВЕРХ СЛОЯ BONDRESEARCH. Колонка rating реестра — та, по
            # которой работают ВСЕ фильтры, и в ней должен лежать минимум по
            # агентствам со ступенью («AA-»), а не склеенный бакет corpbonds
            # («AA»). Без этой проверки два писателя гонялись бы за колонкой:
            # дрейн затирает, воркер ratings-br через 6 часов возвращает — и
            # рейтинг бумаги мигал бы между прогонами. json-кэш выше пишется
            # всегда: он остаётся фолбэком для бумаг, которых слой не знает.
            if reg is not None and bucket != "NR" and not br_covered(isin):
                try:
                    if reg.get(isin):
                        reg.set_rating(isin, bucket)
                except Exception:
                    pass
    _save()
    progress.finish("ratings_drain", detail=f"найдено {n} · промахов {misses} из {len(todo)}")
    logger.info("ratings refresh: +%d rated, %d miss (todo %d)", n, misses, len(todo))
    return n
