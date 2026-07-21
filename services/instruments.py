"""Классификатор бумаг портфеля: corp_fix / corp_float / ofz_pd / ofz_pk / fx_usd / fx_eur / fx_cny.

Источники (в порядке приоритета):
  1. FACEUNIT из справочника MOEX (fetch_moex_securities) — валютные бумаги.
  2. description-блок MOEX (/iss/securities/{isin}.json): TYPE=ofz_bond → ОФЗ;
     SECID SU29*/SU24* → ПК (флоатер), SU25*/SU26* → ПД.
  3. Юниверс флоатеров НРД (fetch_floater_universe) — corp_float.
  4. COUPONTYPE из справочника: 'переменный'/'плавающая' → float, иначе fix.

Кэш description память+диск (instrument_class_cache.json) — справочник стабилен.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional, Set

import httpx

from services.market_data import MarketDataService, _moex_get

from services.paths import cache_path as _cache_path
CLASS_CACHE_FILE = _cache_path("instrument_class_cache.json")

# человекочитаемые ярлыки классов (фронт использует как есть)
CLASS_LABELS = {
    "corp_float": "Корп флоатеры",
    "corp_fix": "Корп фиксы",
    "ofz_pd": "ОФЗ-ПД",
    "ofz_pk": "ОФЗ-ПК",
    "fx_usd": "Валютные USD",
    "fx_eur": "Валютные EUR",
    "fx_cny": "Валютные CNY",
    "unknown": "Прочее",
}

_desc_cache: dict = {}
_desc_loaded = False


def _load_desc_cache() -> None:
    global _desc_loaded
    if _desc_loaded:
        return
    try:
        with open(CLASS_CACHE_FILE, "r", encoding="utf-8") as f:
            _desc_cache.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    _desc_loaded = True


def _save_desc_cache() -> None:
    try:
        from services.paths import atomic_write_json as _awj
        _awj(CLASS_CACHE_FILE, _desc_cache)
    except OSError:
        pass


async def _fetch_descriptions(isins: List[str]) -> Dict[str, dict]:
    """{isin: {'secid','type','shortname'}} через q-поиск ISS (/iss/securities?q=).

    Поиск резолвит ISIN→SECID (board-less endpoint для ОФЗ ISIN не понимает,
    только SU…) и сразу отдаёт type (ofz_bond/corporate_bond/exchange_bond).
    Кэш диск — справочник стабилен."""
    _load_desc_cache()
    # null-записи (старый формат / неудачный резолв) перефетчиваем
    out = {i: _desc_cache[i] for i in isins
           if i in _desc_cache and _desc_cache[i].get("secid")}
    missing = [i for i in isins if i not in out]
    if not missing:
        return out

    async def fetch_one(client: httpx.AsyncClient, isin: str):
        try:
            resp = await _moex_get(
                client, "https://iss.moex.com/iss/securities.json",
                params={"q": isin, "iss.meta": "off", "limit": 10}, timeout=8)
            if resp is None or resp.status_code != 200:
                return
            sec = resp.json().get("securities", {})
            cols, rows = sec.get("columns", []), sec.get("data", [])
            gi = {n: cols.index(n) for n in ("secid", "isin", "type", "shortname") if n in cols}
            for r in rows:
                if (r[gi["isin"]] or "").upper() == isin:
                    out[isin] = {
                        "secid": r[gi["secid"]],
                        "type": r[gi["type"]],
                        "shortname": r[gi["shortname"]],
                    }
                    return
        except Exception:
            pass

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(fetch_one(client, i) for i in missing))
    new = {i: out[i] for i in missing if i in out}
    if new:
        _desc_cache.update(new)
        _save_desc_cache()
    return out


def _classify_one(isin: str, sec: Optional[dict], desc: Optional[dict],
                  floater_isins: Set[str]) -> str:
    # 1) валюта номинала
    face_unit = ((sec or {}).get("face_unit") or "RUB").upper()
    if face_unit in ("USD",):
        return "fx_usd"
    if face_unit in ("EUR",):
        return "fx_eur"
    if face_unit in ("CNY", "CNH"):
        return "fx_cny"

    # 2) ОФЗ: TYPE либо SECID SUxx (secid также есть в справочнике MOEX)
    typ = ((desc or {}).get("type") or "").lower()
    secid = ((desc or {}).get("secid") or (sec or {}).get("secid") or "").upper()
    if typ == "ofz_bond" or secid.startswith("SU"):
        # серии ПК: 24xxx, 29xxx; ПД: 25xxx, 26xxx (плюс редкие 27xxx/28xxx считаем ПК/АД → ПД не точно, фикс по имени)
        if secid.startswith(("SU24", "SU29")):
            return "ofz_pk"
        return "ofz_pd"

    # 3) корп: флоатер по юниверсу НРД, иначе по типу купона
    if isin in floater_isins:
        return "corp_float"
    ctype = ((sec or {}).get("coupon_type") or "").lower()
    if "перемен" in ctype or "плав" in ctype or "float" in ctype:
        return "corp_float"
    if ctype or (sec or {}).get("maturity"):
        return "corp_fix"
    return "unknown"


async def classify(isins: List[str]) -> Dict[str, dict]:
    """{isin: {'cls', 'label', 'ccy', 'name', 'sec'}} — класс + справочник MOEX."""
    if not isins:
        return {}
    from services import nrd as nrd_service
    sec_map, desc_map, uni = await asyncio.gather(
        MarketDataService.fetch_moex_securities(isins),
        _fetch_descriptions(isins),
        nrd_service.fetch_floater_universe(),
    )
    floater_isins = {u.get("isin") for u in (uni or []) if u.get("isin")}
    out: Dict[str, dict] = {}
    for isin in isins:
        sec = sec_map.get(isin)
        desc = desc_map.get(isin)
        cls = _classify_one(isin, sec, desc, floater_isins)
        ccy = ((sec or {}).get("face_unit") or "RUB").upper()
        if ccy in ("CNH",):
            ccy = "CNY"
        elif ccy in ("SUR",):
            ccy = "RUB"
        out[isin] = {
            "cls": cls,
            "label": CLASS_LABELS.get(cls, cls),
            "ccy": ccy,
            "name": (sec or {}).get("name") or (desc or {}).get("shortname") or isin,
            "sec": sec or {},
        }
    return out
