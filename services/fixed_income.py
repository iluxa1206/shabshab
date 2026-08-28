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

import asyncio
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


def build_fixed_cashflows(schedule: dict, calc_date: date,
                          exchange_face=None) -> Tuple[List[tuple], Optional[float], Optional[date]]:
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
        # ПОЛНОТА ГРАФИКА — страховка на будущие обрезы. Корень (пагинация
        # amortizations читалась только с первой страницы) закрыт в
        # market_data.fetch_bond_schedule_full, но если график снова придёт
        # обрезанным, Σ будущих траншей превратится в копейки (sИАДОМ1P19:
        # 8.78 ₽ против биржевых 577.64), а цена котируется в % от БИРЖЕВОГО
        # номинала — купонная доходность раздувается в 1/k раз (до тысяч bps).
        # Тот же guard, что во флоатерах (services/bonds.py:201). НЕ достраиваем
        # residual: put_date у таких бумаг — ближайший НЕОПРЕДЕЛЁННЫЙ купон, а не
        # оферта, и выкуп всего номинала на эту дату дал бы 33-143% годовых.
        if exchange_face and face < float(exchange_face) * 0.95:
            return [], None, None
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
    exchange_face=None,
) -> dict:
    """{'ytm_pct','mod_dur','dv01','g_spread_bps','dirty','face_current','complete'}.

    price_pct — чистая цена в % от остаточного номинала; accrued — НКД в валюте
    номинала на одну бумагу. dirty/dv01 — на одну бумагу в валюте номинала.
    """
    out = {"ytm_pct": None, "mod_dur": None, "mac_dur": None, "convexity": None,
           "dv01": None, "g_spread_bps": None, "dirty": None, "face_current": None,
           "put_date": None}
    cfs, face, put_date = build_fixed_cashflows(schedule, calc_date, exchange_face)
    if face is None:
        # график амортизаций пришёл обрезанным — метрики считать не на чем
        out["incomplete_schedule"] = True
        return out
    out["face_current"] = face
    out["put_date"] = put_date.isoformat() if put_date else None
    if not cfs:
        return out

    dirty = face * price_pct / 100.0 + (accrued or 0.0)
    out["dirty"] = dirty
    if dirty <= 0:
        return out

    # якорь = ДАТА ПОСТАВКИ (T+1 раб; пятница → понедельник): dirty платится
    # на settle, YTM/дюрация считаются от неё — та же конвенция, что у флоатеров
    settle = settle_date(calc_date)
    flows = [(settle, -dirty)] + cfs
    y = xirr(flows)
    if y is None:
        return out
    out["ytm_pct"] = round(y * 100.0, 2)

    # численная дюрация/выпуклость: PV при y±10бп. Якорим поток к settle
    # (xnpv дисконтирует к дате ПЕРВОГО элемента) — иначе PV считается на дату
    # первого купона и pv0≠dirty, что искажает знаменатель выпуклости.
    dy = 0.001
    anchored = [(settle, 0.0)] + cfs
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
        "marketdata.columns": "SECID,LAST,LCURRENTPRICE,WAPRICE,VALTODAY,BID,OFFER",
    }, timeout=20)
    if resp is None or resp.status_code != 200:
        return []
    data = (await asyncio.to_thread(resp.json))
    sec = data.get("securities", {})
    cols, rows = sec.get("columns", []), sec.get("data", [])
    g = lambda row, n: row[cols.index(n)] if n in cols else None
    md = data.get("marketdata", {})
    mcols, mrows = md.get("columns", []), md.get("data", [])
    mg = lambda row, n: row[mcols.index(n)] if n in mcols else None
    last_by: Dict[str, float] = {}
    val_by: Dict[str, float] = {}
    wap_by: Dict[str, float] = {}
    bid_by: Dict[str, float] = {}
    ask_by: Dict[str, float] = {}
    for mr in mrows:
        sid = mg(mr, "SECID")
        if sid:
            px = mg(mr, "LAST") or mg(mr, "LCURRENTPRICE") or mg(mr, "WAPRICE")
            if px is not None:
                last_by[sid] = _numf(px)
            val_by[sid] = _numf(mg(mr, "VALTODAY")) or 0.0
            # средневзвешенная цена дня — отдельным полем, а не только фолбэком
            # для last: на ней стоит аналитика (см. compute_fixed_row)
            wap_by[sid] = _numf(mg(mr, "WAPRICE"))
            # верх стакана из того же снапшота: пустая сторона приходит нулём —
            # это «стороны нет», а не цена 0
            bid_by[sid] = _numf(mg(mr, "BID")) or None
            ask_by[sid] = _numf(mg(mr, "OFFER")) or None
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
            "face": face, "settle_face": settle_face, "linked": linked,
            "faceunit": (g(row, "FACEUNIT") or "").upper(),
            "accrued": _numf(g(row, "ACCRUEDINT")) or 0.0,
            "prev": _numf(g(row, "PREVPRICE")), "prev_date": g(row, "PREVDATE"),
            "last": last_by.get(g(row, "SECID")),
            "wap": wap_by.get(g(row, "SECID")),
            "bid": bid_by.get(g(row, "SECID")), "ask": ask_by.get(g(row, "SECID")),
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


def apply_board_prices(universe: List[dict], snap: Dict[str, dict]) -> None:
    """Свежие цены борд-снапшота MOEX в строки универса (на месте).

    Универс фиксов пересобирается раз в час (_UNI_TTL) — вместе с ценами, что
    в нём лежат. Без этой накладки прогрев метрик считал бы YTM и спреды по
    цене часовой давности, хотя снапшот держится свежим тактом 5с
    (api.main.quotes_poller). Стороны стакана — ПОЛНЫЙ снимок верха: пусто
    значит «стороны нет», поэтому они перезаписываются и пустыми."""
    for u in universe:
        v = snap.get(u.get("isin"))
        if not v:
            continue
        for src, dst in (("last", "last"), ("prev", "prev"), ("prev_date", "prev_date"),
                         ("accrued", "accrued"), ("waprice", "wap")):
            if v.get(src) is not None:
                u[dst] = v[src]
        u["bid"], u["ask"] = v.get("bid"), v.get("ask")
        if v.get("vol") is not None:
            u["val_today"] = v["vol"]


def fixed_side_metrics(row: dict, full: dict, g_curve, calc_date: date,
                       prices, known: Dict[float, dict] = None) -> Dict[float, dict]:
    """{цена: {'ytm','g_spread_bps'}} по произвольным ценам одной бумаги.

    Стороны стакана, средневзвес, VWAP-набор тикета — всё это одна и та же
    бумага по разным ценам: поток, номинал и НКД от цены не зависят, меняется
    только дисконтирование. Считаем ПРЯМЫМ пересчётом на каждой цене, а не
    линеаризацией от last: наклон честен только рядом с якорем, а уехавший
    якорь уводит за собой все производные разом (флоатеры, 27.08.2026).
    Повторы цен схлопываются — bid==last у неликвида обычное дело; known —
    уже посчитанные цены той же бумаги (цена сделки), чтобы не считать дважды."""
    out: Dict[float, dict] = dict(known or {})
    face = row.get("settle_face") or row.get("face")
    accrued = row.get("accrued") or 0.0
    for px in prices:
        if px is None or px <= 0:
            continue
        key = round(float(px), 4)
        if key in out:
            continue
        m = fixed_metrics_from_schedule(full, px, accrued, calc_date, g_curve,
                                        exchange_face=face)
        out[key] = {"ytm": m.get("ytm_pct"), "g_spread_bps": m.get("g_spread_bps")}
    return out


# Своему тиковому средневзвесу верим, пока он покрывает БОЛЬШУЮ ЧАСТЬ дневного
# оборота бумаги. У фиксов в архив тиков пишется только крупняк (порог
# services/trades_stream), поэтому после рестарта дневной счёт поднимается из
# архива неполным — и средневзвес по одним крупным сделкам смещён сильнее, чем
# отстающий, но полный биржевой WAPRICE.
_WAP_COVER_MIN = 0.7


def pick_wap(row: dict) -> Optional[float]:
    """Средневзвешенная цена дня: свой тиковый VWAP, если он покрывает оборот,
    иначе биржевой WAPRICE из снапшота."""
    from services import live_quotes
    lv = live_quotes.get(row.get("isin")) or {}
    own_px, own_val = lv.get("vwap_pct"), lv.get("val_today") or 0.0
    exch_px, exch_val = row.get("wap"), row.get("val_today") or 0.0
    if not own_px:
        return exch_px
    if exch_px and exch_val > 0 and own_val < _WAP_COVER_MIN * exch_val:
        return exch_px
    return own_px


def _static_flags(out: dict, row: dict, full: dict, calc_date: date) -> None:
    """Признаки выпуска, от цены не зависящие: амортизация и свежесть цены.
    Нужны фильтрам витрины, поэтому ставятся и бумаге без метрик."""
    # Амортизация: больше одного транша погашения номинала в графике MOEX
    # (единственная запись — обычное погашение в конце).
    out["has_amort"] = sum(1 for a in (full.get("amorts") or [])
                           if a.get("value") is not None) > 1
    # Тонкая цена: последняя цена MOEX старше 4 дней — бумага не торговалась,
    # метрики сняты с несвежего принта. Правило то же, что у флоатеров
    # (services/universe): возраст PREVDATE, а не NUMTRADES.
    out["price_thin"] = False
    pd = row.get("prev_date")
    if pd:
        try:
            out["price_thin"] = (calc_date - date.fromisoformat(pd)).days > 4
        except (ValueError, TypeError):
            pass


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
    _static_flags(out, row, full, calc_date)
    if px is None or not full.get("coupons"):
        return out
    m = fixed_metrics_from_schedule(full, px, row.get("accrued") or 0.0, calc_date,
                                    g_curve,
                                    # номинал НА ДАТУ ПОСТАВКИ: face — на сегодня,
                                    # а Σ будущих траншей считается от settle, и
                                    # транш в окне (calc, settle] давал ложный отказ
                                    exchange_face=row.get("settle_face") or row.get("face"))
    out.update({
        "ytm": m.get("ytm_pct"), "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
        "convexity": m.get("convexity"), "dv01": m.get("dv01"),
        "g_spread_bps": m.get("g_spread_bps"), "dirty": m.get("dirty"),
        "put_date": m.get("put_date"),
    })
    # МЕТРИКИ ПО ДРУГИМ ЦЕНАМ той же бумаги: средневзвес дня и стороны стакана.
    # Средневзвес — база аналитики: last price это ОДНА сделка, в неликвиде
    # случайный тонкий принт, часто на закрытии. Свой тиковый средневзвес
    # впереди биржевого: WAPRICE из ISS отстаёт. Стороны стакана — то, по чему
    # реально торгуют. Всё считается ПРЯМЫМ пересчётом (см. fixed_side_metrics).
    if price_override is None:
        wap = pick_wap(row)
        bid, ask = row.get("bid"), row.get("ask")
        out["wap_pct"] = wap
        out["bid"] = bid
        out["ask"] = ask
        sides = fixed_side_metrics(
            row, full, g_curve, calc_date, (wap, bid, ask),
            known={round(float(px), 4): {"ytm": m.get("ytm_pct"),
                                         "g_spread_bps": m.get("g_spread_bps")}})
        for price, g_key, y_key in ((wap, "g_spread_wap_bps", "ytm_wap"),
                                    (bid, "g_spread_bid_bps", "ytm_bid"),
                                    (ask, "g_spread_ask_bps", "ytm_ask")):
            m_side = sides.get(round(float(price), 4)) if price else {}
            out[g_key] = (m_side or {}).get("g_spread_bps")
            out[y_key] = (m_side or {}).get("ytm")

    # z-спред над КБД ОФЗ (дискретный, метод НРД) — по тем же потокам
    if g_curve is not None and getattr(g_curve, "ok", lambda: False)() and m.get("dirty"):
        try:
            from services.zspread import solve_z_discrete
            cfs, _face, _put = build_fixed_cashflows(
                full, calc_date, row.get("settle_face") or row.get("face"))
            if cfs:
                out["z_spread_bps"] = solve_z_discrete(g_curve, cfs, calc_date, m["dirty"])
        except Exception as e:
            logger.warning(f"fixed z-spread error {row.get('isin')}: {e}")
    # текущая доходность = годовой купон / чистая цена
    cp = row.get("coupon_pct")
    if cp is not None and px:
        out["cur_yield"] = round(cp / (px / 100.0), 4)
    # Движение к предыдущему закрытию, п.п. — как у флоатеров (services/universe):
    # считаем от СЕГОДНЯШНЕЙ цены, а не от отката на prev-close, иначе бумага
    # без сделок показывала бы аккуратный ноль вместо честного прочерка.
    today_px, prev = row.get("last"), row.get("prev")
    if price_override is None and today_px is not None and prev is not None:
        out["delta_to_prev_close"] = round(float(today_px) - float(prev), 4)
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
    await asyncio.to_thread(MarketDataService.flush_schedule_cache)   # дозапись хвоста дебаунс-кэша

    # ~700 бумаг чистого счёта — в поток, по той же причине, что и метрики
    # флоатеров: в event loop это фриз всего сервера на каждом прогреве.
    def _crunch() -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for u, full in zip(universe, fulls):
            f = {} if isinstance(full, Exception) else (full or {})
            out[u["isin"]] = compute_fixed_row(u, f, g_curve, calc_date)
        return out

    from services.heavy import run_heavy
    return await run_heavy(_crunch)


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
