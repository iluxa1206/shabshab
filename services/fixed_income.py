"""Метрики облигаций с фиксированным купоном: YTM, мод. дюрация, DV01, G-spread.

Кэшфлоу — из реального расписания MOEX bondization (fetch_bond_schedule_full):
купоны с известным value + амортизации. Будущие купоны без value (купон после
оферты не определён) → оценка к оферте (yield-to-put): поток до последнего
известного купона + выкуп остаточного номинала на его дату.

YTM — через xirr (эффективная годовая, ACT/365, как НРД). Дюрация — численная
(bump ±10бп по ставке дисконтирования). G-spread = YTM − КБД(τ=дюрация).
Все расчёты в валюте номинала; конверсия в рубли — на уровне портфеля.
"""
from __future__ import annotations

import re
import json
import time
import logging
from datetime import date
from typing import List, Optional, Tuple, Dict

import httpx

from core.valuation import xirr, xnpv, settle_date
from services.market_data import MarketDataService, _moex_get
from services.paths import cache_path

logger = logging.getLogger(__name__)


def _issuer_of(name: str) -> str:
    """Эмитент из имени выпуска: срезаем хвостовой токен-серию с цифрой
    ('Самолет P13'→'Самолет', 'ЗСД 01'→'ЗСД', 'ОФЗ 26212'→'ОФЗ'). Дёшево, без
    сети (в отличие от MOEX EMITTER_ID)."""
    n = (name or "").strip()
    return re.sub(r"\s+\S*\d\S*\s*$", "", n).strip() or n


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def build_fixed_cashflows(schedule: dict, calc_date: date) -> Tuple[List[tuple], float, Optional[date]]:
    """Будущие кэшфлоу (pay_date, amount) из bondization + остаточный номинал на calc_date.

    Будущие купоны без value (после оферты купон не определён) → поток обрезается
    на последнем ИЗВЕСТНОМ купоне, и на его дату добавляется выкуп остаточного
    номинала (put@100) — стандартный yield-to-worst/to-put для корпов с офертой.
    Возвращает (cfs, current_face, put_date): put_date=None если поток полный
    до погашения (редемпшн из амортизаций bondization).
    """
    coupons: List[tuple] = []      # (date, value) известных будущих купонов
    put_date: Optional[date] = None
    # T+1: купон с pay_date <= settle покупателю не достаётся (ex-coupon, НКД=0)
    settle = settle_date(calc_date)

    # будущие погашения принципала из bondization (весь принципал, включая
    # финальный редемпшн, идёт строками амортизаций)
    future_am = sorted(
        (d, float(a["value"])) for a in schedule.get("amorts", [])
        if a.get("value") is not None and (d := _d(a.get("date"))) and d > settle
    )

    for c in schedule.get("coupons", []):
        end = _d(c.get("end"))
        if end is None or end <= settle:
            continue
        if c.get("value") is None:
            # первый неизвестный купон → оценка к оферте: всё после отбрасываем.
            # если неизвестен уже ближайший купон (оферта вплотную) — путим на его
            # дату (иначе бумага ошибочно оценилась бы к погашению по амортизациям)
            put_date = coupons[-1][0] if coupons else end
            break
        coupons.append((end, float(c["value"])))

    # Остаточный номинал = Σ будущих погашений принципала (надёжно); поле face
    # строк купонов MOEX ненадёжно (для будущих периодов бывает стейл/1000) —
    # используем его только как фолбэк при пустом графике амортизаций.
    if future_am:
        face = sum(v for _, v in future_am)
    else:
        face = None
        for c in schedule.get("coupons", []):
            end = _d(c.get("end"))
            if end and end > settle and c.get("face") is not None:
                face = float(c["face"])
                break
        if face is None:
            face = 1000.0

    cfs = list(coupons)

    if put_date is not None:
        # амортизации ДО оферты остаются в потоке; на оферту — выкуп остатка
        early = [(d, v) for d, v in future_am if d < put_date]
        cfs.extend(early)
        residual = face - sum(v for _, v in early)
        if residual > 1e-9:
            cfs.append((put_date, residual))  # выкуп остатка по номиналу
    else:
        cfs.extend(future_am)

    cfs.sort(key=lambda x: x[0])
    return cfs, face, put_date


def fixed_metrics_from_schedule(
    schedule: dict,
    price_pct: float,
    accrued: float,
    calc_date: date,
    g_curve=None,
) -> dict:
    """{'ytm_pct','mod_dur','dv01','g_spread_bps','dirty','face_current','complete'}.

    price_pct — чистая цена в % от остаточного номинала; accrued — НКД в валюте
    номинала на одну бумагу. dirty/dv01 — на одну бумагу в валюте номинала.
    """
    out = {"ytm_pct": None, "mod_dur": None, "mac_dur": None, "convexity": None,
           "dv01": None, "g_spread_bps": None, "dirty": None, "face_current": None,
           "put_date": None}
    cfs, face, put_date = build_fixed_cashflows(schedule, calc_date)
    out["face_current"] = face
    out["put_date"] = put_date.isoformat() if put_date else None
    if not cfs:
        return out

    dirty = face * price_pct / 100.0 + (accrued or 0.0)
    out["dirty"] = dirty
    if dirty <= 0:
        return out

    flows = [(calc_date, -dirty)] + cfs
    y = xirr(flows)
    if y is None:
        return out
    out["ytm_pct"] = round(y * 100.0, 2)

    # численная дюрация/выпуклость: PV при y±10бп. Якорим поток к calc_date
    # (xnpv дисконтирует к дате ПЕРВОГО элемента) — иначе PV считается на дату
    # первого купона и pv0≠dirty, что искажает знаменатель выпуклости.
    dy = 0.001
    anchored = [(calc_date, 0.0)] + cfs
    try:
        pv_dn = xnpv(y - dy, anchored)
        pv_up = xnpv(y + dy, anchored)
        pv0 = xnpv(y, anchored)  # == dirty по построению xirr
    except ValueError:
        return out
    if pv_dn <= 0 or pv_up <= 0 or pv0 <= 0:
        return out
    mod_dur = (pv_dn - pv_up) / (2.0 * pv0 * dy)
    out["mod_dur"] = round(mod_dur, 2)
    out["mac_dur"] = round(mod_dur * (1.0 + y), 2)  # Маколей при эффективной годовой
    out["dv01"] = round(mod_dur * dirty * 1e-4, 4)  # ₽(валюта)/бумагу на 1бп
    out["convexity"] = round((pv_dn + pv_up - 2.0 * pv0) / (pv0 * dy * dy), 2)

    if g_curve is not None and getattr(g_curve, "ok", lambda: False)():
        # тенор КБД матчим по Маколею (как НРД), не по модифицированной
        tau = max(mod_dur * (1.0 + y), 0.01)
        out["g_spread_bps"] = round((y - g_curve.r(tau)) * 10000.0)
    return out


async def fixed_metrics(isin: str, price_pct: float, accrued: float,
                        calc_date: date, g_curve=None) -> dict:
    """Обёртка: тянет bondization из кэша MarketDataService и считает метрики."""
    schedule = await MarketDataService.fetch_bond_schedule_full(isin)
    return fixed_metrics_from_schedule(schedule, price_pct, accrued, calc_date, g_curve)


# ─────────────────────────── Универс ФИКСОВ ───────────────────────────
# Борды MOEX: TQOB — ОФЗ (берём ПД, серии SU25/SU26), TQCB — корпораты.
_BOARDS = {"TQOB": "ofz", "TQCB": "corp"}
_UNI_TTL = 3600.0
_CORP_CAP = 700          # максимум корпоратов (топ по обороту) — bounds прогрев
_uni_mem: dict = {"ts": 0.0, "rows": None}
# отсекаем по имени: валютные/замещающие (не прямой рублёвый фикс)
_SKIP_NAME = ("CNY", "USD", "EUR", "GLD", "ЗАМ", "ЗО2", "ЗО3")


def _numf(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


async def _fetch_fixed_board(client, board: str) -> List[dict]:
    """Строки борда (securities+marketdata одним запросом): справка + цена."""
    url = f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/{board}/securities.json"
    resp = await _moex_get(client, url, params={
        "iss.only": "securities,marketdata",
        "securities.columns": "SECID,ISIN,SHORTNAME,MATDATE,COUPONPERCENT,"
                              "FACEVALUE,FACEVALUEONSETTLEDATE,FACEUNIT,ACCRUEDINT,PREVPRICE,PREVDATE",
        "marketdata.columns": "SECID,LAST,LCURRENTPRICE,WAPRICE,VALTODAY",
    }, timeout=20)
    if resp is None or resp.status_code != 200:
        return []
    data = resp.json()
    sec = data.get("securities", {})
    cols, rows = sec.get("columns", []), sec.get("data", [])
    g = lambda row, n: row[cols.index(n)] if n in cols else None
    md = data.get("marketdata", {})
    mcols, mrows = md.get("columns", []), md.get("data", [])
    mg = lambda row, n: row[mcols.index(n)] if n in mcols else None
    last_by: Dict[str, float] = {}
    val_by: Dict[str, float] = {}
    for mr in mrows:
        sid = mg(mr, "SECID")
        if sid:
            px = mg(mr, "LAST") or mg(mr, "LCURRENTPRICE") or mg(mr, "WAPRICE")
            if px is not None:
                last_by[sid] = _numf(px)
            val_by[sid] = _numf(mg(mr, "VALTODAY")) or 0.0
    out = []
    for row in rows:
        isin = g(row, "ISIN")
        if not isin:
            continue
        nm = g(row, "SHORTNAME") or isin
        face = _numf(g(row, "FACEVALUE")) or 1000.0
        settle_face = _numf(g(row, "FACEVALUEONSETTLEDATE"))
        # ЛИНКЕР: номинал РАСТЁТ день ко дню (индексируется по RUONIA/инфляции) →
        # номинал на дату сеттла > текущего. У фикса он постоянен, у амортизации
        # не растёт. Тогда фикс-купон начисляется на индексир. номинал, а YTM =
        # РЕАЛЬНАЯ доходность (спред над базой).
        linked = bool(settle_face and face and settle_face > face * 1.0002)
        out.append({
            "isin": isin, "secid": g(row, "SECID"),
            "name": nm, "issuer": _issuer_of(nm),
            "maturity_date": g(row, "MATDATE") or None,
            "coupon_pct": _numf(g(row, "COUPONPERCENT")),
            "face": face, "linked": linked,
            "faceunit": (g(row, "FACEUNIT") or "").upper(),
            "accrued": _numf(g(row, "ACCRUEDINT")) or 0.0,
            "prev": _numf(g(row, "PREVPRICE")), "prev_date": g(row, "PREVDATE"),
            "last": last_by.get(g(row, "SECID")),
            "val_today": val_by.get(g(row, "SECID")) or 0.0, "board": board,
        })
    return out


def _is_fixed(row: dict, board: str, floaters: set) -> bool:
    """Рублёвый фикс? ОФЗ-ПД — серии SU25/SU26 (SU29=ПК, SU52=ИН отсекаются).
    Корпорат — купон>0 и НЕ известный флоатер (реестр)."""
    if row["isin"] in floaters:
        return False
    if (row.get("secid") or "").startswith("BYM"):
        return False  # РесБел (Белоруссия) — квазисуверен под санкциями, вне скоупа
    # только рублёвые: FACEUNIT=SUR/RUB (валютные/замещающие исключаем)
    fu = row.get("faceunit") or ""
    if fu and fu not in ("SUR", "RUB", "RUR"):
        return False
    name = (row.get("name") or "").upper()
    if any(s in name for s in _SKIP_NAME):
        return False
    cp = row.get("coupon_pct")
    if cp is None or cp <= 0 or not row.get("maturity_date"):
        return False
    if board == "TQOB":
        return (row.get("secid") or "")[:4] in ("SU25", "SU26")
    return True


_LIQ_VAL = 1_000_000.0   # порог оборота, ₽/день

def _liquid(row: dict, today: date) -> bool:
    """Ликвидная: оборот сегодня ≥ 1млн ₽ ИЛИ торговалась за последние 5 дней
    (prev_date). Стабильно вне торгов (по prev_date), отсекает мёртвый неликвид."""
    if (row.get("val_today") or 0.0) >= _LIQ_VAL:
        return True
    pd = row.get("prev_date")
    if row.get("prev") is None or not pd:
        return False
    try:
        return (today - date.fromisoformat(pd)).days <= 5
    except (ValueError, TypeError):
        return False


async def fetch_fixed_universe() -> List[dict]:
    """Универс фиксов: ОФЗ-ПД (TQOB) + ликвидные корпораты (TQCB), только с ценой
    (last/prev). Флоатеры исключены по реестру. Кэш в памяти на час."""
    now = time.time()
    if _uni_mem["rows"] is not None and now - _uni_mem["ts"] < _UNI_TTL:
        return _uni_mem["rows"]
    from services import instruments_registry as reg
    # не только KEYRATE/RUONIA: base NULL (флоатер без параметров) и EXOTIC тоже
    # флоатеры — узкий фильтр пропускал их сюда как «фиксы» с ложным YTM
    floaters = reg.non_fixed_isins()
    today = date.today()
    rows: List[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            for board in _BOARDS:
                for r in await _fetch_fixed_board(client, board):
                    if not _is_fixed(r, board, floaters):
                        continue
                    if not _liquid(r, today):
                        continue
                    r["cls"] = _BOARDS[board]
                    rows.append(r)
    except Exception as e:
        logger.warning(f"fixed universe error: {e}")
        return _uni_mem["rows"] or []
    # ОФЗ держим все; линкеры (индекс. номинал) тоже держим все — их мало и они
    # редкие; корпораты — топ по обороту (кап bounds прогрев поллера).
    ofz = [r for r in rows if r["cls"] == "ofz"]
    linked = [r for r in rows if r["cls"] == "corp" and r.get("linked")]
    corp = sorted((r for r in rows if r["cls"] == "corp" and not r.get("linked")),
                  key=lambda r: r.get("val_today") or 0.0, reverse=True)[:_CORP_CAP]
    rows = ofz + linked + corp
    _uni_mem["rows"] = rows
    _uni_mem["ts"] = now
    return rows


def compute_fixed_row(row: dict, full: dict, g_curve, calc_date: date,
                      price_override: float = None) -> dict:
    """Полный набор метрик фикс-бумаги для строки таблицы: цена (last→prev),
    YTM/тек.доходность/g-спред/z-спред/дюрация/convexity/DV01.

    price_override — чистая цена калькулятора карточки: считает все метрики под
    произвольную цену вместо рыночной (last→prev). НКД/поток не зависят от цены."""
    if price_override is not None:
        px = price_override
        out = {"last": px, "prev": row.get("prev"), "price_stale": False}
    else:
        px = row.get("last") if row.get("last") is not None else row.get("prev")
        out = {"last": px, "prev": row.get("prev"),
               "price_stale": row.get("last") is None and row.get("prev") is not None}
    if px is None or not full.get("coupons"):
        return out
    m = fixed_metrics_from_schedule(full, px, row.get("accrued") or 0.0, calc_date, g_curve)
    out.update({
        "ytm": m.get("ytm_pct"), "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
        "convexity": m.get("convexity"), "dv01": m.get("dv01"),
        "g_spread_bps": m.get("g_spread_bps"), "dirty": m.get("dirty"),
        "put_date": m.get("put_date"),
    })
    # z-спред над КБД ОФЗ (дискретный, метод НРД) — по тем же потокам
    if g_curve is not None and getattr(g_curve, "ok", lambda: False)() and m.get("dirty"):
        try:
            from services.zspread import solve_z_discrete
            cfs, _face, _put = build_fixed_cashflows(full, calc_date)
            if cfs:
                out["z_spread_bps"] = solve_z_discrete(g_curve, cfs, calc_date, m["dirty"])
        except Exception as e:
            logger.warning(f"fixed z-spread error {row.get('isin')}: {e}")
    # текущая доходность = годовой купон / чистая цена
    cp = row.get("coupon_pct")
    if cp is not None and px:
        out["cur_yield"] = round(cp / (px / 100.0), 4)
    return out


async def compute_fixed_metrics_all(universe: List[dict], g_curve, calc_date: date) -> Dict[str, dict]:
    """{isin: метрики} по всему универсу фиксов. Расписания MOEX (bondization)
    батчатся из day-кэша; фоновый прогрев — в универс-поллере."""
    import asyncio
    universe = [u for u in universe if u.get("isin")]
    if not universe:
        return {}
    # расписание MOEX тянем по SECID: у ОФЗ ISIN (RU000…) в bondization не
    # резолвится, а SECID (SU26…) — да. У корпов SECID обычно = ISIN.
    fulls = await asyncio.gather(
        *(MarketDataService.fetch_bond_schedule_full(u.get("secid") or u["isin"]) for u in universe),
        return_exceptions=True)
    out: Dict[str, dict] = {}
    for u, full in zip(universe, fulls):
        if isinstance(full, Exception):
            full = {}
        out[u["isin"]] = compute_fixed_row(u, full or {}, g_curve, calc_date)
    return out


_YTM_HIST_FILE = cache_path("fixed_ytm_history.json")


def apply_ytm_delta(metrics: Dict[str, dict], today_iso: str) -> None:
    """Проставляет delta_ytm (изменение YTM к предыдущему торговому дню, п.п.) в
    каждую метрику и сохраняет сегодняшний срез YTM на диск. Храним ~40 дней."""
    try:
        with open(_YTM_HIST_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        hist = {}
    prev_days = sorted(d for d in hist if d < today_iso)
    prev = hist.get(prev_days[-1], {}) if prev_days else {}
    for isin, m in metrics.items():
        y, p = m.get("ytm"), prev.get(isin)
        if y is not None and p is not None:
            m["delta_ytm"] = round(y - p, 2)
    hist[today_iso] = {i: m["ytm"] for i, m in metrics.items() if m.get("ytm") is not None}
    for d in sorted(hist)[:-40]:
        hist.pop(d, None)
    try:
        with open(_YTM_HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f)
    except OSError as e:
        logger.warning(f"ytm history save failed: {e}")
