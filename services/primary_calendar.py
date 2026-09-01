"""Календарь анонсов первички (планируемые размещения) с bondresearch.ru.

Источник: https://www.bondresearch.ru/boards/calendar_final.json — тот же сайт,
что уже питает слой спек фиксинга (services/bondresearch), позиционный массив
из 14 полей под ключом "demo". Их собственный дашборд показывает 11 колонок из
14; ориентир доходности/дюрации (12/13) и ссылку на новость (11) они держат в
данных, но не рисуют — мы рисуем.

Собственного расчёта здесь НЕТ: это чистая витрина чужого анонса до того, как
выпуск появится на бирже (ISIN ещё не существует, привязать к реестру не к
чему). Как только бумага разместится, она приезжает в универс обычным
дискавери и живёт в мониторе — вкладка «Первичка» её больше не касается.

Обновление: раз в сутки из дневного синка + ленивый TTL на запросе. Запрос
УСЛОВНЫЙ (If-None-Match / If-Modified-Since) — сайт отдаёт etag+last-modified,
304 стоит нам ноль трафика и оставляет кэш как есть.

Кэш durable (data/cache/primary_calendar.json, том переживает редеплой) и хранит
first_seen на строку: анонс, которого вчера не было, помечается «новый» —
именно ради этого вкладку и заводили.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx

from services import paths

logger = logging.getLogger(__name__)

URL = os.getenv("PRIMARY_CALENDAR_URL",
                "https://www.bondresearch.ru/boards/calendar_final.json")
CACHE_FILE = "primary_calendar.json"
TTL_SEC = int(os.getenv("PRIMARY_CALENDAR_TTL_H", "6")) * 3600
# Сколько дней строка считается «новой» с момента первого появления в выгрузке.
NEW_DAYS = int(os.getenv("PRIMARY_CALENDAR_NEW_DAYS", "3"))

# индексы позиционного массива (их <th> в corporate_calendar.html)
(I_ISSUE, I_BOOK, I_ISSUER, I_RATING, I_CCY, I_TERM, I_VOL,
 I_FREQ, I_COUPON, I_TECH, I_COMMENT, I_URL, I_YTM, I_DUR) = range(14)

# Рейтинги на сайте написаны СМЕШАННОЙ раскладкой («ВВ-» кириллицей, «BBB+»
# латиницей) — без нормализации одинаковые буквы сортируются и фильтруются как
# разные символы.
_CYR2LAT = str.maketrans("АВСЕ", "ABCE")

# Санити-порог: пустой/куцый ответ не должен затирать рабочий кэш.
_MIN_EXPECTED = 3


def _s(v) -> str | None:
    s = (v or "").strip() if isinstance(v, str) else None
    return s or None


def _num(v):
    """Первое число из строки: «2'000»→2000, «1,5»→1.5, «≥ 1'000»→1000,
    «26,83 - 28,71»→26.83 (диапазон — по нижней границе, сырое поле рядом)."""
    if v is None:
        return None
    t = str(v).replace("'", "").replace(" ", "").replace(" ", "")
    m = re.search(r"-?\d+(?:[.,]\d+)?", t)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _key(r: dict) -> str:
    """Идентичность анонса для переноса first_seen между выгрузками. ISIN'а ещё
    нет; серия (первый токен комментария, «002Р-01, call-опцион…») + эмитент
    устойчивы, а даты книги/размещения по анонсу как раз и ездят."""
    series = (r.get("comment") or "").split(",")[0].strip()
    return f"{r.get('issuer', '')}|{series}"


def parse_rows(rows: list) -> list[dict]:
    out = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 14:
            continue
        issuer = _s(r[I_ISSUER])
        if not issuer:
            continue
        rating = _s(r[I_RATING])
        out.append({
            "issue_date": _s(r[I_ISSUE]),           # дата размещения
            "book_date": _s(r[I_BOOK]),             # дата сбора книги
            "issuer": issuer,
            "ratings": [x for x in (rating or "").translate(_CYR2LAT).split("/")
                        if x and x != "-"],
            "rating_raw": rating,
            "currency": _s(r[I_CCY]) or "RUB",
            "term_years": _num(r[I_TERM]),          # срок обращения / до оферты
            "term_raw": _s(r[I_TERM]),
            "volume_mln": _num(r[I_VOL]),
            "volume_raw": _s(r[I_VOL]),             # «≥ 1'000» — ориентир, не факт
            "coupon_freq": _s(r[I_FREQ]),
            "coupon_guide": _s(r[I_COUPON]),        # ориентир ставки словами
            "is_floater": _s(r[I_TECH]) == "1",     # их поле frn_technical
            "comment": _s(r[I_COMMENT]),            # серия, оферта/поручитель
            "url": _s(r[I_URL]),
            "ytm_pct": _num(r[I_YTM]),              # только у фиксов
            "ytm_raw": _s(r[I_YTM]),
            "duration_years": _num(r[I_DUR]),
        })
    return out


def _read_cache() -> dict:
    import json
    try:
        with open(paths.cache_path(CACHE_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _merge(fresh: list[dict], prev: list[dict], now_iso: str) -> list[dict]:
    """Перенос first_seen со старой выгрузки; новым строкам — сегодняшняя дата.

    ПЕРВЫЙ ПОСЕВ (кэша не было) — first_seen=None: иначе на холодном старте вся
    таблица красится «новое», и метка обесценивается ровно там, где она нужна."""
    seed = not prev
    seen = {_key(r): r.get("first_seen") for r in prev}
    for r in fresh:
        r["first_seen"] = None if seed else (seen.get(_key(r)) or now_iso)
    return fresh


async def refresh(force: bool = False) -> dict:
    """Условный GET → мердж → durable-кэш. Возвращает статистику.
    Сбой сети/сайта не роняет вкладку: кэш остаётся прежним."""
    cached = _read_cache()
    age = time.time() - float(cached.get("fetched_ts") or 0)
    if not force and cached.get("rows") and age < TTL_SEC:
        return {"status": "fresh", "rows": len(cached["rows"]), "age_sec": int(age)}

    headers = {"User-Agent": "shabshab-desk/1.0"}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(URL, headers=headers, timeout=30)

    now = datetime.now(timezone.utc)
    if resp.status_code == 304:
        cached["fetched_ts"] = now.timestamp()
        cached["fetched_at"] = now.isoformat(timespec="seconds")
        paths.atomic_write_json(paths.cache_path(CACHE_FILE), cached)
        return {"status": "not_modified", "rows": len(cached.get("rows") or [])}
    resp.raise_for_status()

    payload = resp.json()
    raw = payload.get("demo") if isinstance(payload, dict) else payload
    fresh = parse_rows(raw)
    if len(fresh) < _MIN_EXPECTED and cached.get("rows"):
        logger.warning("primary_calendar: куцый ответ (%d строк) — кэш не трогаем", len(fresh))
        return {"status": "too_small", "rows": len(cached["rows"]), "fetched": len(fresh)}

    prev = cached.get("rows") or []
    doc = {
        "rows": _merge(fresh, prev, now.date().isoformat()),
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
        "fetched_ts": now.timestamp(),
        "fetched_at": now.isoformat(timespec="seconds"),
        "source_url": URL,
    }
    paths.atomic_write_json(paths.cache_path(CACHE_FILE), doc)
    added = len([r for r in doc["rows"] if r["first_seen"] == now.date().isoformat()])
    return {"status": "updated", "rows": len(doc["rows"]), "added": added}


async def get_calendar() -> dict:
    """Витрина: строки + метаданные свежести. Ленивое обновление по TTL;
    падение источника отдаёт последний известный кэш (stale лучше пустого)."""
    try:
        await refresh()
    except Exception as e:                                   # noqa: BLE001
        logger.warning("primary_calendar refresh failed: %s", e)
    doc = _read_cache()
    rows = doc.get("rows") or []
    today = datetime.now(timezone.utc).date()
    for r in rows:
        fs = r.get("first_seen")
        try:
            r["is_new"] = fs is not None and (today - datetime.fromisoformat(fs).date()).days < NEW_DAYS
        except (TypeError, ValueError):
            r["is_new"] = False
    rows.sort(key=lambda r: (r.get("book_date") or r.get("issue_date") or "9999",
                             r.get("issuer") or ""))
    return {
        "rows": rows,
        "fetched_at": doc.get("fetched_at"),
        "source_url": doc.get("source_url") or URL,
        "source_name": "bondresearch.ru",
    }
