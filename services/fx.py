"""Курсы валют для пересчёта валютных бумаг в рубли.

Основной источник — MOEX валютный рынок, расчёты TOM (USD000UTSTOM / CNYRUB_TOM /
EUR_RUB__TOM): LAST → WAPRICE → PREVPRICE. Кэш память TTL 60с — кнопка «пересчёт»
на фронте получает свежий курс без бомбёжки ISS.

Фолбэк — официальный ЦБ (XML_daily, фиксируется на день): недостающие валюты
(неликвидный EUR TOM, выходные) добираются оттуда. Совсем всё упало → stale-кэш
с диска (fx_cache.json): старый курс лучше пустого NAV.
"""
from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from datetime import date
from typing import Dict, Optional

import httpx

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
MOEX_CETS_URL = "https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities.json"
FX_CACHE_FILE = "fx_cache.json"

_TOM_SECIDS = {"USD000UTSTOM": "USD", "CNYRUB_TOM": "CNY", "EUR_RUB__TOM": "EUR"}
_CCYS = {"USD", "EUR", "CNY"}
_TOM_TTL = 60.0  # сек

# {"ts": monotonic, "rates": {...}, "source": {ccy: "tom"|"cbr"}, "label": str}
_mem: dict = {"ts": 0.0, "data": None}
_cbr_mem: dict = {"date": None, "rates": None}
# последний УСПЕШНЫЙ TOM: эпизодический таймаут ISS не должен ронять курс в
# дневной ЦБ — 15-минутный стейл TOM ближе к рынку
_tom_last: dict = {"ts": 0.0, "rates": None, "upd": None}
_TOM_STALE_OK = 900.0  # сек


def _load_disk() -> Optional[dict]:
    try:
        with open(FX_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_disk(data: dict) -> None:
    try:
        with open(FX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _parse_cbr_xml(raw: bytes) -> Dict[str, float]:
    """{'USD': 78.5, ...} — Value/Nominal (у CNY номинал может быть 10).
    Вход bytes: XML ЦБ несёт encoding="windows-1251" в декларации — str ET не ест."""
    out: Dict[str, float] = {}
    root = ET.fromstring(raw)
    for v in root.iter("Valute"):
        code = (v.findtext("CharCode") or "").upper()
        if code not in _CCYS:
            continue
        try:
            nominal = float((v.findtext("Nominal") or "1").replace(",", "."))
            value = float((v.findtext("Value") or "0").replace(",", "."))
            if nominal > 0 and value > 0:
                out[code] = value / nominal
        except ValueError:
            continue
    return out


async def _fetch_tom(client: httpx.AsyncClient) -> tuple[Dict[str, float], Optional[str]]:
    """Курсы TOM с MOEX: LAST → WAPRICE → PREVPRICE. Возвращает (rates, updatetime)."""
    rates: Dict[str, float] = {}
    upd: Optional[str] = None
    resp = await client.get(MOEX_CETS_URL, params={
        "iss.meta": "off", "securities": ",".join(_TOM_SECIDS)}, timeout=8)
    resp.raise_for_status()
    j = resp.json()
    sec = j.get("securities", {})
    md = j.get("marketdata", {})
    prev_by = {}
    sc, sd = sec.get("columns", []), sec.get("data", [])
    if sd and "SECID" in sc and "PREVPRICE" in sc:
        si, pi = sc.index("SECID"), sc.index("PREVPRICE")
        prev_by = {r[si]: r[pi] for r in sd}
    mc, mdata = md.get("columns", []), md.get("data", [])
    gi = {n: mc.index(n) for n in ("SECID", "LAST", "WAPRICE", "UPDATETIME") if n in mc}
    for r in mdata:
        secid = r[gi["SECID"]]
        ccy = _TOM_SECIDS.get(secid)
        if not ccy:
            continue
        v = r[gi.get("LAST")] if "LAST" in gi else None
        if v is None and "WAPRICE" in gi:
            v = r[gi["WAPRICE"]]
        if v is None:
            v = prev_by.get(secid)
        if v is not None and float(v) > 0:
            rates[ccy] = float(v)
            t = r[gi["UPDATETIME"]] if "UPDATETIME" in gi else None
            if t and (upd is None or t > upd):
                upd = t
    return rates, upd


async def _fetch_cbr(client: httpx.AsyncClient) -> Dict[str, float]:
    today = date.today().isoformat()
    if _cbr_mem["date"] == today and _cbr_mem["rates"]:
        return _cbr_mem["rates"]
    resp = await client.get(CBR_URL, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    rates = _parse_cbr_xml(resp.content)
    if rates:
        _cbr_mem.update({"date": today, "rates": rates})
    return rates


async def get_fx() -> dict:
    """{'rates': {'RUB':1,'USD':..,..}, 'source': {ccy: 'tom'|'cbr'}, 'label': 'TOM 18:23'}.

    TOM (TTL 60с) + добор ЦБ; stale-фолбэк с диска. label — для UI («чем посчитан NAV»).
    """
    now = time.monotonic()
    if _mem["data"] and now - _mem["ts"] < _TOM_TTL:
        return _mem["data"]

    rates: Dict[str, float] = {}
    source: Dict[str, str] = {}
    label = None

    async with httpx.AsyncClient() as client:
        try:
            tom, upd = await _fetch_tom(client)
            if tom:
                _tom_last.update({"ts": now, "rates": dict(tom), "upd": upd})
        except Exception as e:
            print(f"MOEX TOM FX error: {e!r}")
            tom, upd = {}, None
        if not tom and _tom_last["rates"] and now - _tom_last["ts"] < _TOM_STALE_OK:
            tom, upd = _tom_last["rates"], _tom_last["upd"]
        for ccy, v in tom.items():
            rates[ccy] = v
            source[ccy] = "tom"
        if tom:
            label = "TOM" + (f" {upd[:5]}" if upd else "")
        missing = _CCYS - set(rates)
        if missing:
            try:
                cbr = await _fetch_cbr(client)
                for ccy in missing:
                    if ccy in cbr:
                        rates[ccy] = cbr[ccy]
                        source[ccy] = "cbr"
                if not label and any(s == "cbr" for s in source.values()):
                    label = "ЦБ " + date.today().strftime("%d.%m")
            except Exception as e:
                print(f"CBR FX fetch error: {e}")

    if rates:
        rates["RUB"] = 1.0
        data = {"rates": rates, "source": source, "label": label or "?"}
        _mem.update({"ts": now, "data": data})
        _save_disk(data)
        return data

    # stale: диск → память → голый рубль
    disk = _load_disk()
    if disk and disk.get("rates"):
        disk["label"] = (disk.get("label") or "?") + " (stale)"
        return disk
    if _mem["data"]:
        return _mem["data"]
    return {"rates": {"RUB": 1.0}, "source": {}, "label": "нет данных"}


async def get_fx_rates() -> Dict[str, float]:
    """Совместимость: только словарь курсов."""
    return (await get_fx())["rates"]
