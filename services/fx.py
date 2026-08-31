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
from datetime import date, timedelta
from typing import Dict, Optional

import httpx
import logging

logger = logging.getLogger(__name__)

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
MOEX_CETS_URL = "https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities.json"
from services.paths import cache_path as _cache_path
FX_CACHE_FILE = _cache_path("fx_cache.json")

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
        from services.paths import atomic_write_json as _awj
        _awj(FX_CACHE_FILE, data)
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
            logger.warning(f"MOEX TOM FX error: {e!r}")
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
                logger.warning(f"CBR FX fetch error: {e}")

    if rates:
        rates["RUB"] = 1.0
        data = {"rates": rates, "source": source, "label": label or "?"}
        _mem.update({"ts": now, "data": data})
        _save_disk(data)
        _remember(rates, source)
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


# ─────────────────────────── архив курсов по дням ───────────────────────────
# Зачем: рублёвая величина за ПРОШЛУЮ дату (объём сделки по замещайке — цена
# идёт процентом от валютного номинала) не считается сегодняшним курсом.
# Живой срез выше знает только «сейчас», поэтому фиксируем курс дня в базе:
# вперёд — сами, назад — историей ЦБ (backfill_cbr).
_ARCHIVE_MIN_SEC = 600          # чаще раза в 10 минут день переписывать незачем
_arch: dict = {"at": 0.0, "day": None}
# id валют в динамике ЦБ (XML_dynamic.asp): свои, не совпадают с кодом
_CBR_IDS = {"USD": "R01235", "EUR": "R01239", "CNY": "R01375"}
CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
MOEX_HISTORY_URL = ("https://iss.moex.com/iss/history/engines/currency/markets/selt/boards/CETS/securities")
# ЦБ отдаёт динамику только «браузеру»: без User-Agent приходит 403
_UA = "Mozilla/5.0 (compatible; floaters-desk/1.0)"


def _remember(rates: Dict[str, float], source: Dict[str, str]) -> None:
    """Кладёт сегодняшний курс в архив. Дебаунс по времени: get_fx зовётся
    десятками раз в минуту, а запись нужна раз в несколько минут — день всё
    равно перезаписывается последним значением.

    Ошибку записи глотаем: курс для расчётов уже получен, а падать из-за
    занятой базы слой котировок не должен."""
    now = time.monotonic()
    today = date.today().isoformat()
    if _arch["day"] == today and now - _arch["at"] < _ARCHIVE_MIN_SEC:
        return
    try:
        save_rates(today, rates, source)
    except Exception as e:
        logger.warning("fx archive: %s", e)
        return
    _arch.update({"at": now, "day": today})


def save_rates(day: str, rates: Dict[str, float],
               source: Optional[Dict[str, str]] = None) -> int:
    """Курсы одного дня в архив. Последнее значение дня побеждает — курс TOM
    ходит внутри дня, и «курс дня» у нас это его последний известный уровень."""
    source = source or {}
    rows = [(day, ccy, float(v), source.get(ccy), _now_str())
            for ccy, v in (rates or {}).items()
            if ccy != "RUB" and v and float(v) > 0]
    if not rows:
        return 0
    from services.portfolio_db import _connect, _lock
    with _lock, _connect() as c:
        c.executemany(
            "INSERT INTO fx_rate(date,ccy,rate,source,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(date,ccy) DO UPDATE SET rate=excluded.rate, "
            "source=excluded.source, updated_at=excluded.updated_at", rows)
    return len(rows)


def _now_str() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")


def rates_by_day(ccy: str, frm: str, till: str) -> Dict[str, float]:
    """{'YYYY-MM-DD': курс} за окно. Выходных и праздников в архиве нет —
    протяжку последнего известного делает потребитель (так же, как с номиналом
    амортизируемой бумаги)."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute(
            "SELECT date, rate FROM fx_rate WHERE ccy=? AND date>=? AND date<=? "
            "ORDER BY date", (ccy.upper(), frm, till)).fetchall()
        out = {r["date"]: r["rate"] for r in rows}
        # курс НА НАЧАЛО окна: сделка в понедельник считается пятничным курсом,
        # если понедельника в архиве ещё нет
        prev = c.execute("SELECT date, rate FROM fx_rate WHERE ccy=? AND date<? "
                         "ORDER BY date DESC LIMIT 1", (ccy.upper(), frm)).fetchone()
    if prev:
        out.setdefault(prev["date"], prev["rate"])
    return out


def rate_on(ccy: str, day: str) -> Optional[float]:
    """Курс на дату: точный или последний известный до неё."""
    ccy = ccy.upper()
    if ccy in ("RUB", "SUR", "RUR", ""):
        return 1.0
    from services.portfolio_db import _connect
    with _connect() as c:
        r = c.execute("SELECT rate FROM fx_rate WHERE ccy=? AND date<=? "
                      "ORDER BY date DESC LIMIT 1", (ccy, day)).fetchone()
    return r["rate"] if r else None


def archive_stats() -> dict:
    """Глубина архива курсов — для /api/status и отладки."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute("SELECT ccy, COUNT(*) n, MIN(date) d0, MAX(date) d1 "
                         "FROM fx_rate GROUP BY ccy ORDER BY ccy").fetchall()
    return {r["ccy"]: {"n": r["n"], "from": r["d0"], "till": r["d1"]} for r in rows}


def _parse_cbr_dynamic(raw: bytes) -> Dict[str, float]:
    """XML динамики ЦБ по ОДНОЙ валюте → {'YYYY-MM-DD': курс}."""
    out: Dict[str, float] = {}
    root = ET.fromstring(raw)
    for rec in root.iter("Record"):
        d = rec.get("Date") or ""
        try:
            nominal = float((rec.findtext("Nominal") or "1").replace(",", "."))
            value = float((rec.findtext("Value") or "0").replace(",", "."))
        except ValueError:
            continue
        if not d or nominal <= 0 or value <= 0:
            continue
        dd, mm, yy = d.split(".")
        out[f"{yy}-{mm}-{dd}"] = value / nominal
    return out


async def _moex_history(client: httpx.AsyncClient, secid: str,
                        frm: str, till: str) -> Dict[str, float]:
    """{'YYYY-MM-DD': курс} из дневной истории валютной пары MOEX.

    Берём WAPRICE (средневзвешенная дня), CLOSE — запасной: живой курс мы тоже
    считаем по TOM, и история из того же инструмента не создаёт ступеньки между
    «сегодня» и «вчера», как было бы с курсом ЦБ."""
    out: Dict[str, float] = {}
    start = 0
    while True:
        r = await client.get(
            f"{MOEX_HISTORY_URL}/{secid}.json",
            params={"from": frm, "till": till, "start": start, "iss.meta": "off",
                    "iss.only": "history",
                    "history.columns": "TRADEDATE,WAPRICE,CLOSE"}, timeout=25)
        r.raise_for_status()
        h = r.json().get("history", {})
        cols, rows = h.get("columns", []), h.get("data", [])
        if not rows:
            return out
        di, wi, ci = cols.index("TRADEDATE"), cols.index("WAPRICE"), cols.index("CLOSE")
        for row in rows:
            v = row[wi] or row[ci]
            if row[di] and v and float(v) > 0:
                out[row[di]] = float(v)
        if len(rows) < 100:
            return out
        start += len(rows)


async def backfill_history(days: int = 400) -> dict:
    """История курсов за окно назад → архив. Источник — MOEX (тот же TOM, что у
    живого курса), недостающие дни добираем у ЦБ.

    Выходных нет ни там, ни там (курс не торгуется) — дыры закрывает протяжка
    последнего известного на чтении, а не выдуманные строки."""
    till = date.today()
    frm = till - timedelta(days=max(days, 1))
    saved = 0
    got: Dict[str, Dict[str, float]] = {}
    async with httpx.AsyncClient() as client:
        for secid, ccy in _TOM_SECIDS.items():
            try:
                hist = await _moex_history(client, secid, frm.isoformat(),
                                           till.isoformat())
            except Exception as e:
                logger.warning("fx history %s: %s", ccy, e)
                hist = {}
            got[ccy] = hist
            for day, rate in hist.items():
                saved += save_rates(day, {ccy: rate}, {ccy: "tom"})
        # ЦБ — вторым заходом и только по валютам, где MOEX не дал ничего
        # (неликвидная пара, сбой ISS). Он же покрывает выходные своим
        # официальным курсом, но ступеньку с TOM мы предпочитаем не плодить.
        for ccy, vid in _CBR_IDS.items():
            if got.get(ccy):
                continue
            try:
                r = await client.get(CBR_DYNAMIC_URL, timeout=20, follow_redirects=True,
                                     headers={"User-Agent": _UA},
                                     params={"date_req1": frm.strftime("%d/%m/%Y"),
                                             "date_req2": till.strftime("%d/%m/%Y"),
                                             "VAL_NM_RQ": vid})
                r.raise_for_status()
                hist = _parse_cbr_dynamic(r.content)
            except Exception as e:
                logger.warning("fx backfill cbr %s: %s", ccy, e)
                continue
            for day, rate in hist.items():
                saved += save_rates(day, {ccy: rate}, {ccy: "cbr"})
    return {"saved": saved, "from": frm.isoformat(), "till": till.isoformat(),
            "archive": archive_stats()}
