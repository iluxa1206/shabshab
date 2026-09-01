"""Рейтинги ПО АГЕНТСТВАМ с bondresearch.ru — приоритетный слой над corpbonds.

Что даёт этот источник, чего не даёт corpbonds:
  • разбивка по агентствам (Эксперт РА / АКРА) вместо одной склеенной строки;
  • ДАТА присвоения каждой оценки — видно, насколько она несвежая;
  • отозванные рейтинги помечены явно и не выдаются за действующие.

Устройство. Снимка «эмитент → текущий рейтинг» у сайта нет: есть ЛЕНТА
СОБЫТИЙ ratings_change.json (2600 записей с 2007 г.: агентство, дата,
текущий/предыдущий, тип). Текущая оценка = последнее по дате событие пары
(эмитент, агентство); события типа «Отозван» гасят пару целиком — иначе
отозванный рейтинг вечно висел бы действующим.

Лента знает только НАЗВАНИЕ эмитента. ISIN добирается из двух витрин сайта:
base_test.json (фиксы) и pig_floaters_mk.json (флоатеры, его же читает
services/bondresearch ради спек фиксинга). Связка идёт по названию эмитента:
issuer_code в ленте и в витринах — из РАЗНЫХ пространств («A03» против «BGW94»)
и не бьётся.

Рейтинг бумаги = ХУДШАЯ из оценок агентств (ratings.rating_min): инвестор
ограничен низшей оценкой, а не самой лестной. Именно эта величина едет в
колонку rating реестра, поэтому ВСЕ существующие фильтры (чипы грейдов, меню
ступеней, отбор сигналов, разрезы спредов) начинают работать «по минимуму» без
единой правки в них самих.

Покрытие на 2026-09-01: 763 наших бумаги из 1173 есть в витринах сайта, ~646
получают оценку хотя бы одного из двух агентств. Заменить corpbonds этим слоем
НЕЛЬЗЯ: 214 наших бумаг есть только там — поэтому слой приоритетный, но не
единственный (см. ratings.bucket_of).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx

from services import paths
from services.ratings import rating_min, rating_norm, rating_to_bucket

logger = logging.getLogger(__name__)

BASE = "https://www.bondresearch.ru/boards/"
URL_CHANGES = BASE + "ratings_change.json"
URL_FIXED = BASE + "base_test.json"       # витрина фиксов: ISIN + эмитент
URL_FLOATERS = BASE + "pig_floaters_mk.json"

CACHE_FILE = "ratings_br.json"
TTL_SEC = 12 * 3600

# Позиционные индексы чужих выгрузок (сверены на данных 2026-09-01). Колонку у
# себя они могут переставить в любой день, поэтому значения ПРОВЕРЯЮТСЯ по
# форме (_ISIN_RE ниже): молча связать ISIN не с тем эмитентом — худшее, что
# может сделать этот слой, такая ошибка не видна ни в логе, ни на витрине.
CH_ISSUER, CH_AGENCY, CH_DATE, CH_CURRENT, CH_ACTION = 0, 1, 2, 3, 5
FX_ISSUER, FX_ISIN = 26, 39
FL_ISIN, FL_ISSUER = 1, 24

# Берём только эти два. НКР/НРА в ленте есть (и НКР покрывает заметно больше
# бумаг, чем НРА), но в витрину пока не выводятся — решение продуктовое, а не
# техническое: добавить агентство здесь и в AGENCIES достаточно.
AGENCIES = {"Эксперт РА": "expert", "АКРА": "acra"}
_WITHDRAWN = "Отозван"
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

# Санити: сайт может отдать пустой/куцый файл — не затирать рабочий кэш.
_MIN_CHANGES = 500
_MIN_ISINS = 300


def parse_current(changes: list) -> dict:
    """Лента событий → {эмитент: {agency_key: {"rating", "date"}}}.

    Последнее событие пары по дате побеждает; «Отозван» удаляет пару. Оценки
    вне шкалы (мусор, «Withdrawn» текстом) отбрасываются rating_norm."""
    latest: dict = {}
    for e in changes or []:
        try:
            issuer = (e[CH_ISSUER] or "").strip()
            agency = AGENCIES.get((e[CH_AGENCY] or "").strip())
            day = (e[CH_DATE] or "").strip()
        except (IndexError, TypeError):
            continue
        if not issuer or not agency or not day:
            continue
        key = (issuer, agency)
        prev = latest.get(key)
        if prev is None or day >= prev[CH_DATE]:
            latest[key] = e

    out: dict = {}
    for (issuer, agency), e in latest.items():
        if (e[CH_ACTION] or "").strip() == _WITHDRAWN:
            continue
        val = rating_norm(e[CH_CURRENT])
        if not val:
            continue
        out.setdefault(issuer, {})[agency] = {"rating": val, "date": e[CH_DATE]}
    return out


def parse_isin_map(fixed_rows: list, floater_rows: list) -> dict:
    """{ISIN: эмитент} из двух витрин сайта."""
    out = {}
    for rows, i_isin, i_issuer in ((fixed_rows, FX_ISIN, FX_ISSUER),
                                   (floater_rows, FL_ISIN, FL_ISSUER)):
        for r in rows or []:
            try:
                isin = (r[i_isin] or "").strip()
                issuer = (r[i_issuer] or "").strip()
            except (IndexError, TypeError, AttributeError):
                continue
            # форма ISIN — единственная защита от переставленной колонки
            if issuer and _ISIN_RE.match(isin):
                out[isin] = issuer
    return out


def build(changes: list, fixed_rows: list, floater_rows: list) -> dict:
    """{ISIN: {agencies:{...}, rating, bucket, issuer}} — rating есть ХУДШАЯ
    из оценок агентств."""
    by_issuer = parse_current(changes)
    isin_map = parse_isin_map(fixed_rows, floater_rows)
    out = {}
    for isin, issuer in isin_map.items():
        ag = by_issuer.get(issuer)
        if not ag:
            continue
        worst = rating_min(*(v["rating"] for v in ag.values()))
        if not worst:
            continue
        out[isin] = {"issuer": issuer, "agencies": ag,
                     "rating": worst, "bucket": rating_to_bucket(worst)}
    return out


def _extract(payload):
    if isinstance(payload, dict):
        for k in ("demo", "data"):
            if isinstance(payload.get(k), list):
                return payload[k]
        return []
    return payload if isinstance(payload, list) else []


async def fetch() -> dict:
    """Три выгрузки сайта → карта рейтингов. Сеть; исключения наружу."""
    headers = {"User-Agent": "shabshab-desk/1.0"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        import asyncio
        rs = await asyncio.gather(client.get(URL_CHANGES, headers=headers),
                                  client.get(URL_FIXED, headers=headers),
                                  client.get(URL_FLOATERS, headers=headers))
    for r in rs:
        r.raise_for_status()
    changes, fixed_rows, floater_rows = (_extract(r.json()) for r in rs)
    if len(changes) < _MIN_CHANGES:
        raise ValueError(f"куцая лента рейтингов: {len(changes)} событий")
    return build(changes, fixed_rows, floater_rows)


def _read_cache() -> dict:
    import json
    try:
        with open(paths.cache_path(CACHE_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_mem: dict | None = None


def _doc() -> dict:
    global _mem
    if _mem is None:
        _mem = _read_cache()
    return _mem


def ratings_of(isin: str) -> dict | None:
    """{agencies, rating, bucket} одной бумаги — для карточки и колонки."""
    return (_doc().get("map") or {}).get(isin)


def ratings_map(isins) -> dict:
    """{isin: {...}} батчем — витрины зовут именно это."""
    m = _doc().get("map") or {}
    return {i: m[i] for i in isins if i in m}


def ea_map(isins) -> dict:
    """{isin: "A+/A"} — Эксперт РА / АКРА через слэш, прочерк на месте
    отсутствующей оценки. Строку собирает бэкенд, а не витрина: порядок
    агентств — часть контракта колонки (заголовок «INSTRUMENT (Э/А)»), и
    разъехаться он не должен между таблицей, лентой и карточкой."""
    m = _doc().get("map") or {}
    out = {}
    for i in isins:
        r = m.get(i)
        if not r:
            continue
        ag = r.get("agencies") or {}
        e = (ag.get("expert") or {}).get("rating") or "-"
        a = (ag.get("acra") or {}).get("rating") or "-"
        if e != "-" or a != "-":
            out[i] = f"{e}/{a}"
    return out


def bucket_map(isins) -> dict:
    m = _doc().get("map") or {}
    return {i: m[i]["bucket"] for i in isins if i in m and m[i].get("bucket")}


async def refresh(force: bool = False) -> dict:
    """Обновить карту и записать durable-кэш. Куцый ответ рабочий кэш не
    затирает: пустая колонка рейтингов хуже вчерашней."""
    global _mem
    doc = _doc()
    age = time.time() - float(doc.get("fetched_ts") or 0)
    if not force and doc.get("map") and age < TTL_SEC:
        return {"status": "fresh", "isins": len(doc["map"]), "age_sec": int(age)}

    fresh_map = await fetch()
    if len(fresh_map) < _MIN_ISINS and doc.get("map"):
        logger.warning("ratings_br: куцая карта (%d ISIN) — кэш не трогаем", len(fresh_map))
        return {"status": "too_small", "isins": len(doc["map"]), "fetched": len(fresh_map)}

    now = datetime.now(timezone.utc)
    new_doc = {"map": fresh_map, "fetched_ts": now.timestamp(),
               "fetched_at": now.isoformat(timespec="seconds")}
    paths.atomic_write_json(paths.cache_path(CACHE_FILE), new_doc)
    _mem = new_doc
    return {"status": "updated", "isins": len(fresh_map)}


def apply_to_registry() -> dict:
    """Записать худший рейтинг в колонку rating реестра — ту самую, по которой
    работают все фильтры. Только для известных нам ISIN и только там, где
    значение реально меняется."""
    from services import instruments_registry as reg
    m = _doc().get("map") or {}
    if not m:
        return {"written": 0, "skipped": "пустая карта"}
    known = {r["isin"] for r in reg.universe_rows(only_priceable=False, only_floaters=False)}
    cur = reg.ratings_map(sorted(known))
    written = 0
    for isin in known & set(m):
        val = m[isin]["rating"]
        if cur.get(isin) != val:
            try:
                reg.set_rating(isin, val)
                written += 1
            except Exception as e:                              # noqa: BLE001
                logger.warning("ratings_br: %s не записан: %s", isin, e)
    return {"matched": len(known & set(m)), "written": written}
