"""Метрики облигаций с фиксированным купоном: YTM, мод. дюрация, DV01, G-spread.

Кэшфлоу — из реального расписания MOEX bondization (fetch_bond_schedule_full):
купоны с известным value + амортизации. Будущий купон без value: ставка
объявлена (ABS/ИЦБ) → сумма по ставке и номиналу в обращении; ставка не
объявлена → последний известный купон продлевается до оферты MOEX (yield-to-put,
выкуп остатка номинала на дату выкупа), а без будущей оферты — до погашения,
как MOEX (см. _offer_horizon).

Бессрочные (MOEX MATDATE 0000-00-00) — рента: опубликованные купоны + последний
известный купон, продлённый вперёд, без номинала (см. _perpetual_coupons).

YTM — через xirr (эффективная годовая, ACT/365, как НРД). Дюрация — численная
(bump ±10бп по ставке дисконтирования). G-spread = YTM − КБД(τ=дюрация).
Y-IDX = YTM − доходность роллирования RUONIA до конца потока — та же база и
та же формула, что у флоатеров (services/valuation), поэтому спреды двух классов
сопоставимы в одной витрине (docs/unified_monitor_tz.md).
Все расчёты в валюте номинала, включая НКД (см. _accrued_face_ccy); конверсия в
рубли — на уровне портфеля.
"""
from __future__ import annotations

import asyncio
import os
import re
import json
import time
import logging
from collections import OrderedDict
from datetime import date, timedelta
from typing import List, Optional, Tuple, Dict

import httpx

from core.forwards import add_months
from core.valuation import (xirr, xnpv, settle_date, accrued_at, accrued_estimate,
                            ruonia_rolling_yield_pct)
from services.market_data import moex_client, MarketDataService, _moex_get, CALL_OFFER_SOURCE
from services.paths import cache_path

logger = logging.getLogger(__name__)


# YTM — самый дорогой кусок витрины: один уровень цены запускает xirr и три
# xnpv. Котировка приходит раз в несколько секунд, но у большинства выпусков
# цена между тиками не меняется. Кэшируем только точную пару «поток + цена»,
# без округления/интерполяции и без кривой: кривая может обновиться независимо
# от стакана, поэтому G-spread ниже всегда собирается из актуальной кривой.
_PRICE_METRICS_CACHE_MAX = 20_000
_price_metrics_cache: OrderedDict[tuple, dict] = OrderedDict()
_z_spread_cache: OrderedDict[tuple, Optional[int]] = OrderedDict()
_CASHFLOW_TEMPLATE_CACHE_MAX = 3_000
_cashflow_template_cache: OrderedDict[tuple, tuple] = OrderedDict()


def clear_price_metrics_cache() -> None:
    """Сброс для тестов и явной инвалидации при необходимости."""
    _price_metrics_cache.clear()
    _z_spread_cache.clear()
    _cashflow_template_cache.clear()


def _schedule_cache_key(schedule: dict) -> tuple:
    """Содержательный ключ потока, а не id объекта: расписание может обновиться
    в течение одного процесса после корректировки MOEX.

    Всё, из чего build_fixed_cashflows собирает поток: start и valueprc считают
    купон с объявленной ставкой, но без суммы, а оферты решают, где поток рвётся
    на неопределённом купоне (синтетический колл реестра подмешивается к
    расписанию после кэша, см. market_data._with_call_offers)."""
    coupons = tuple((c.get("start"), c.get("end"), c.get("value"), c.get("valueprc"),
                     c.get("face"))
                    for c in (schedule.get("coupons") or []))
    amorts = tuple((a.get("date"), a.get("value"))
                   for a in (schedule.get("amorts") or []))
    offers = tuple((o.get("date"), o.get("source"))
                   for o in (schedule.get("offers") or []))
    return coupons, amorts, offers


def _price_metrics_key(row: dict, schedule: dict, price_pct: float,
                       accrued: float, calc_date: date, exchange_face) -> tuple:
    return (
        row.get("isin"),
        float(price_pct),
        float(accrued or 0.0),
        calc_date.isoformat(),
        float(exchange_face) if exchange_face is not None else None,
        _is_perpetual(row),
        _schedule_cache_key(schedule),
    )


def _cashflow_template_key(schedule: dict, calc_date: date, exchange_face,
                           perpetual: bool = False) -> tuple:
    return (
        calc_date.isoformat(),
        float(exchange_face) if exchange_face is not None else None,
        perpetual,
        _schedule_cache_key(schedule),
    )


def _cashflows_at_date(schedule: dict, calc_date: date, exchange_face,
                       perpetual: bool = False):
    """Неизменяемый шаблон потока на выпуск/дату расчёта.

    На новом уровне BID/ASK меняется только dirty price. Разбор купонов,
    амортизаций и оферты повторно не делаем; сами доходности ниже по-прежнему
    решаются точно для каждой новой цены.
    """
    key = _cashflow_template_key(schedule, calc_date, exchange_face, perpetual)
    cached = _cashflow_template_cache.get(key)
    if cached is None:
        cfs, face, put_date, assumed_from = build_fixed_cashflows(
            schedule, calc_date, exchange_face, perpetual=perpetual, with_assumed=True)
        cached = (tuple(cfs), face, put_date, assumed_from)
        _cashflow_template_cache[key] = cached
        if len(_cashflow_template_cache) > _CASHFLOW_TEMPLATE_CACHE_MAX:
            _cashflow_template_cache.popitem(last=False)
    else:
        _cashflow_template_cache.move_to_end(key)
    return cached


def _index_yield_pct(ru_curve, row: dict, calc_date: date,
                     flow_end: Optional[date]) -> Optional[float]:
    """База Y-IDX: доходность роллирования RUONIA calc_date → конец потока, %.

    None там, где сравнение с RUONIA не имеет смысла: бессрочная (горизонта
    нет — рента на 100 лет), линкер (YTM у него реальная, а RUONIA номинальная)
    и валютный номинал (RUONIA рублёвая — тот же guard, что у КБД). Путь
    роллирования кэшируется на объекте кривой (_RuoniaCompoundPath), поэтому
    вызов на каждый уровень цены дешёв."""
    if (ru_curve is None or flow_end is None or _is_perpetual(row)
            or row.get("linked") or not _ruble_face(row)):
        return None
    try:
        return ruonia_rolling_yield_pct(ru_curve, calc_date, flow_end)
    except Exception as e:
        logger.warning(f"fixed RUONIA rolling-yield error {row.get('isin')}: {e}")
        return None


def _metrics_at_price(row: dict, schedule: dict, price_pct: float, accrued: float,
                      calc_date: date, g_curve, exchange_face, ru_curve=None) -> dict:
    """Точные метрики уровня цены с LRU базовой математики.

    В кэше лежит результат без КБД и с внутренними неокруглёнными y/duration.
    Это позволяет не гонять xirr/xnpv повторно, но пересчитывать G-spread
    буквально по той же формуле при каждой новой версии кривой. Y-IDX — так же:
    база от цены не зависит, вычитается из неокруглённой YTM.
    """
    key = _price_metrics_key(row, schedule, price_pct, accrued, calc_date, exchange_face)
    base = _price_metrics_cache.get(key)
    if base is None:
        base = fixed_metrics_from_schedule(
            schedule, price_pct, accrued, calc_date, exchange_face=exchange_face,
            perpetual=_is_perpetual(row), _include_raw=True,
        )
        _price_metrics_cache[key] = base
        if len(_price_metrics_cache) > _PRICE_METRICS_CACHE_MAX:
            _price_metrics_cache.popitem(last=False)
    else:
        _price_metrics_cache.move_to_end(key)

    # Не отдаём служебные неокруглённые значения в API/внутренние строки.
    out = {k: v for k, v in base.items() if not k.startswith("_")}
    y = base.get("_ytm_raw")
    mod_dur = base.get("_mod_dur_raw")
    if (y is not None and mod_dur is not None and g_curve is not None
            and getattr(g_curve, "ok", lambda: False)()):
        tau = max(mod_dur * (1.0 + y), 0.01)
        out["g_spread_bps"] = round((y - g_curve.r(tau)) * 10000.0)
    out["yield_over_index_bps"] = out["index_yield_pct"] = None
    if y is not None and ru_curve is not None:
        base_pct = _index_yield_pct(ru_curve, row, calc_date, base.get("_flow_end"))
        if base_pct is not None:
            # формула флоатеров (services/valuation): % − %, в бп
            out["yield_over_index_bps"] = round((y * 100.0 - base_pct) * 100.0)
            out["index_yield_pct"] = round(base_pct, 4)
    return out


def _g_curve_cache_key(g_curve) -> Optional[tuple]:
    """Точный отпечаток КБД для кэша Z-спреда.

    Рабочая GCurve хранит именно эти точки. Если передали совместимый, но другой
    объект кривой, не рискуем старым Z — просто не кэшируем его.
    """
    try:
        return tuple(g_curve.xs), tuple(g_curve.ys)
    except (AttributeError, TypeError):
        return None


def _z_spread_at_price(row: dict, schedule: dict, price_pct: float, accrued: float,
                       calc_date: date, g_curve, exchange_face, dirty: float) -> Optional[int]:
    """Точный Z-спред с кэшем по цене, потоку и всем узлам КБД."""
    curve_key = _g_curve_cache_key(g_curve)
    perpetual = _is_perpetual(row)
    if curve_key is None:
        return _solve_z_spread(schedule, calc_date, g_curve, exchange_face, dirty, perpetual)
    key = (_price_metrics_key(row, schedule, price_pct, accrued, calc_date, exchange_face),
           curve_key)
    if key in _z_spread_cache:
        _z_spread_cache.move_to_end(key)
        return _z_spread_cache[key]
    value = _solve_z_spread(schedule, calc_date, g_curve, exchange_face, dirty, perpetual)
    _z_spread_cache[key] = value
    if len(_z_spread_cache) > _PRICE_METRICS_CACHE_MAX:
        _z_spread_cache.popitem(last=False)
    return value


def _solve_z_spread(schedule: dict, calc_date: date, g_curve, exchange_face,
                    dirty: float, perpetual: bool = False) -> Optional[int]:
    """Один фактический прогон Z-солвера, вынесен для точного LRU выше."""
    from services.zspread import solve_z_discrete
    cfs, _face, _put, _assumed = _cashflows_at_date(schedule, calc_date, exchange_face, perpetual)
    return solve_z_discrete(g_curve, cfs, calc_date, dirty) if cfs else None


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


def _is_perpetual(row: dict) -> bool:
    """Бессрочная: MOEX отдаёт MATDATE '0000-00-00'. Универс нормализует это
    в флаг perpetual (maturity_date=None), сырую дату понимаем тоже — строка
    могла прийти мимо _fetch_fixed_board."""
    return bool(row.get("perpetual")) or str(row.get("maturity_date") or "").startswith("0000")


# Горизонт ренты бессрочной бумаги. Хвост за ним весит (1+y)^-T от цены: при
# 6 % годовых (самая низкая доходность среди бессрочных на 29.09.2026) это
# 0.3 % цены и ~2 бп доходности — дешевле, чем тащить в каждый солвер
# (xirr/xnpv/Z) отдельную формулу терминальной стоимости.
_PERPETUAL_YEARS = 100


def _face_from_coupons(schedule: dict, settle: date) -> float:
    """Номинал из строк купонов — когда графика амортизаций нет (бессрочная)
    или он пуст. Поле face у MOEX для будущих периодов ненадёжно, поэтому это
    только фолбэк."""
    for c in schedule.get("coupons", []):
        end = _d(c.get("end"))
        if end and end > settle and c.get("face") is not None:
            return float(c["face"])
    return 1000.0


def _perpetual_coupons(coupons: list, settle: date) -> List[tuple]:
    """Поток бессрочной бумаги: купоны графика MOEX + последний известный купон,
    продлённый с тем же шагом на _PERPETUAL_YEARS вперёд. Номинала в потоке нет.

    Неопределённые купоны графика (ставка после пересмотра не объявлена)
    заполняются последним известным: так же считает MOEX (купон / цена), только
    мы дисконтируем честно, а не делим. Колл эмитента не предполагаем — у
    бессрочных он в воле эмитента, и ниже номинала его никто не исполнит.
    """
    rows = sorted(((e, _d(c.get("start")), c.get("value")) for c in coupons
                   if (e := _d(c.get("end")))), key=lambda r: r[0])
    out: List[tuple] = []
    last_val, last_end, step = None, None, None
    for end, start, val in rows:
        if val is not None:
            last_val = float(val)
            if start and end > start:
                # шаг в календарных месяцах: 91–92 дня → 3, 182–184 → 6
                step = max(1, round((end - start).days / 30.4375))
        if end > settle and last_val is not None:
            out.append((end, last_val))
        last_end = end
    if last_val is None or last_end is None:
        return []
    step = step or 6
    horizon = add_months(settle, 12 * _PERPETUAL_YEARS)
    k = 1
    while (d := add_months(last_end, step * k)) <= horizon:
        if d > settle:
            out.append((d, last_val))
        k += 1
    return out


# Оферта MOEX чуть раньше даты последнего известного купона ещё относится к
# границе неопределённого купона: выкуп обычно ПОСЛЕ купона, но бывает и в день
# купона. Оферты задолго до неё — прошлый пересмотр ставки: после них купоны уже
# объявлены (Роделен2P1: оферта 01.10.2026, ставка — до 21.07.2027).
_OFFER_BEFORE_COUPON_DAYS = 10


def _offer_horizon(schedule: dict, settle: date, last_end: Optional[date]) -> Optional[date]:
    """Дата выкупа MOEX, к которой считать поток, когда ставка купона после
    last_end не объявлена: первая будущая оферта на этой границе или позже.
    None — такой оферты нет, и последний известный купон продлевается до
    погашения (как MOEX в YIELD).

    Раньше любой неопределённый купон считался офертой на дате последнего
    известного купона. Без оферты это мусор: РКСОлмA902 189 % при 16.35 у
    MOEX, Сбер2СУБ3R 57 % при 10.06, АЛЬФАЗО350 225 % при 7.51, iХРОМОС Б1
    35 % при 12.90 (у него оферты только прошлые — будущей никто не объявлял).
    С офертой — дата купона вместо даты выкупа, а MOEX выкупает на 2–14 дней
    позже (110 из 119 бумаг снимка 29.09.2026): на коротких офертах это до
    2.5 п.п. (ГПБ001P26P: 17.07 к купону, 14.57 к выкупу — ровно YIELDTOOFFER).

    Только оферты MOEX. Синтетический колл реестра (market_data._with_call_offers)
    здесь не горизонт: колл — опцион ЭМИТЕНТА, по правилу горизонта проекта он в
    силе, лишь когда цена выше цены выкупа, а шаблон потока от цены не зависит.
    АЛЬФАЗО350 — колл ровно на границе (26.10), цена 92: рынок отзыва не ждёт,
    выкуп по 100 через 4 недели дал бы те же 225 %.
    """
    lo = (last_end or settle) - timedelta(days=_OFFER_BEFORE_COUPON_DAYS)
    after = sorted(d for o in (schedule.get("offers") or [])
                   if o.get("source") != CALL_OFFER_SOURCE
                   and (d := _d(o.get("date")))            # '0000-00-00' → None
                   and d > settle and d >= lo)
    return after[0] if after else None


def build_fixed_cashflows(schedule: dict, calc_date: date,
                          exchange_face=None,
                          perpetual: bool = False, *,
                          with_assumed: bool = False):
    """Будущие кэшфлоу (pay_date, amount) из bondization + остаточный номинал на calc_date.

    Купоны без value:
      * ставка объявлена (valueprc), сумма нет — ABS/ИЦБ, где сумма зависит от
        будущей амортизации пула: сумма = номинал в обращении × ставка × дни/365;
      * ставка не объявлена — последний известный купон продлевается (сумма
        пересчитана на длину периода и номинал в обращении) до оферты MOEX, где
        остаток номинала выкупается по 100 (put_date), а без будущей оферты —
        до погашения (см. _offer_horizon).
    Возвращает (cfs, current_face, put_date): put_date=None если поток полный
    до погашения (редемпшн из амортизаций bondization). with_assumed=True —
    четвёртым элементом дата, с которой купон ПРЕДПОЛОЖЕН (продлён), или None.

    perpetual — бессрочная: поток-рента без номинала (_perpetual_coupons).
    """
    coupons: List[tuple] = []      # (date, value) будущих купонов
    put_date: Optional[date] = None
    assumed_from: Optional[date] = None
    # T+1: купон с pay_date <= settle покупателю не достаётся (ex-coupon, НКД=0)
    settle = settle_date(calc_date)

    def _ret(cfs, face, put):
        return (cfs, face, put, assumed_from) if with_assumed else (cfs, face, put)

    if perpetual:
        # БЕССРОЧНАЯ: в графике MOEX нет ни траншей погашения, ни неопределённых
        # купонов — общий путь ниже отдавал в XIRR одни известные купоны без
        # номинала, и YTM уходил в −99 % (ВТБСУБ1-11 при цене 63: −99.4 против
        # 15.87 у MOEX). Выкуп номинала на дату последнего купона тоже неверен:
        # это была бы оферта, которой у бессрочной нет.
        return _ret(_perpetual_coupons(schedule.get("coupons") or [], settle),
                    _face_from_coupons(schedule, settle), None)

    # погашения принципала из bondization (весь принципал, включая финальный
    # редемпшн, идёт строками амортизаций); прошлые нужны номиналу в обращении
    # на начало уже идущего купонного периода
    all_am = sorted(
        (d, float(a["value"])) for a in schedule.get("amorts", [])
        if a.get("value") is not None and (d := _d(a.get("date")))
    )
    future_am = [(d, v) for d, v in all_am if d > settle]

    def _outstanding(start: date) -> Optional[float]:
        """Номинал в обращении на купонный период с началом start (транш в день
        окончания периода ещё в обращении). None — графика амортизаций нет."""
        return sum(v for d, v in all_am if d > start) if all_am else None

    last_known = None          # (start, end, сумма) последнего купона с известной суммой
    undefined_seen = False     # встречен купон с необъявленной ставкой
    for c in sorted(schedule.get("coupons", []), key=lambda c: c.get("end") or ""):
        end = _d(c.get("end"))
        if end is None:
            continue
        start = _d(c.get("start"))
        if c.get("value") is not None:
            last_known = (start, end, float(c["value"]))
            if end > settle:
                coupons.append((end, float(c["value"])))
            continue
        if end <= settle:
            continue
        rate = _numf(c.get("valueprc"))
        if rate and start and end > start:
            # ставка объявлена, сумма нет (ABS/ИЦБ): после полного погашения
            # по графику номинала в обращении нет — и купона тоже
            out = _outstanding(start)
            amount = (out if out is not None else _face_from_coupons(schedule, settle)) \
                * rate / 100.0 * (end - start).days / 365.0
            last_known = (start, end, amount)
            if amount > 0:
                coupons.append((end, amount))
            continue
        if not undefined_seen:
            undefined_seen = True
            put_date = _offer_horizon(schedule, settle, last_known[1] if last_known else None)
        if put_date is not None and end > put_date:
            break              # купон за офертой предъявившему бумагу не достаётся
        if last_known is None or (put_date is None and not future_am):
            # продлевать нечего (ни одного известного купона) или некуда (нет
            # графика погашения — поток остался бы без номинала): честный
            # прочерк вместо выдуманного потока
            return _ret([], _face_from_coupons(schedule, settle), None)
        if assumed_from is None:
            assumed_from = start or last_known[1]
        amount = _extended_coupon(last_known, start, end, _outstanding)
        if amount > 0:
            coupons.append((end, amount))

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
            assumed_from = None
            return _ret([], None, None)
    else:
        face = _face_from_coupons(schedule, settle)

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
    return _ret(cfs, face, put_date)


def _extended_coupon(last_known: tuple, start: Optional[date], end: date,
                     outstanding) -> float:
    """Купон с необъявленной ставкой = последний известный, пересчитанный на
    длину периода и номинал в обращении (методика MOEX: ставка последнего
    известного купона до погашения). При равных периодах без амортизации —
    буквально та же сумма."""
    l_start, l_end, l_val = last_known
    if not (start and l_start and l_end > l_start and end > start):
        return l_val
    out_last, out_now = outstanding(l_start), outstanding(start)
    if out_last is None:            # графика амортизаций нет — номинал не меняется
        scale = 1.0
    elif out_last <= 0:
        return 0.0
    else:
        scale = out_now / out_last
    return l_val * scale * (end - start).days / (l_end - l_start).days


def fixed_metrics_from_schedule(
    schedule: dict,
    price_pct: float,
    accrued: float,
    calc_date: date,
    g_curve=None,
    exchange_face=None,
    perpetual: bool = False,
    _include_raw: bool = False,
) -> dict:
    """{'ytm_pct','mod_dur','dv01','g_spread_bps','dirty','face_current','complete'}.

    price_pct — чистая цена в % от остаточного номинала; accrued — НКД в валюте
    номинала на одну бумагу. dirty/dv01 — на одну бумагу в валюте номинала.
    perpetual — бессрочная: YTM ренты (см. build_fixed_cashflows).
    coupon_assumed_from — с этой даты ставка не объявлена и в потоке продлён
    последний известный купон: число держится на допущении, фронт это пишет.
    """
    out = {"ytm_pct": None, "mod_dur": None, "mac_dur": None, "convexity": None,
           "dv01": None, "g_spread_bps": None, "dirty": None, "face_current": None,
           "put_date": None, "coupon_assumed_from": None}
    cfs, face, put_date, assumed_from = _cashflows_at_date(schedule, calc_date,
                                                           exchange_face, perpetual)
    if face is None:
        # график амортизаций пришёл обрезанным — метрики считать не на чем
        out["incomplete_schedule"] = True
        return out
    out["face_current"] = face
    out["put_date"] = put_date.isoformat() if put_date else None
    out["coupon_assumed_from"] = assumed_from.isoformat() if assumed_from else None
    if not cfs:
        return out

    dirty = face * price_pct / 100.0 + (accrued or 0.0)
    out["dirty"] = dirty
    if dirty <= 0:
        return out

    # якорь = ДАТА ПОСТАВКИ (T+1 раб; пятница → понедельник): dirty платится
    # на settle, YTM/дюрация считаются от неё — та же конвенция, что у флоатеров
    settle = settle_date(calc_date)
    flows = [(settle, -dirty), *cfs]
    # У ренты корень всегда > 0 (при y ≤ 0 сумма купонов бесконечна), а нижняя
    # граница xirr по умолчанию (−0.9999) на столетнем потоке даёт 1e-400 в
    # знаменателе — ZeroDivisionError.
    y = xirr(flows, low=0.0) if perpetual else xirr(flows)
    if y is None:
        return out
    out["ytm_pct"] = round(y * 100.0, 2)

    # численная дюрация/выпуклость: PV при y±10бп. Якорим поток к settle
    # (xnpv дисконтирует к дате ПЕРВОГО элемента) — иначе PV считается на дату
    # первого купона и pv0≠dirty, что искажает знаменатель выпуклости.
    dy = 0.001
    anchored = [(settle, 0.0), *cfs]
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

    if _include_raw:
        # Внутреннее значение для точного G-spread из LRU-кэша: публичная YTM
        # остаётся округлённой, как и раньше.
        out["_ytm_raw"] = y
        out["_mod_dur_raw"] = mod_dur
        # конец потока — горизонт базы Y-IDX (поток режется пут-офертой и
        # продлением неопределённого купона до выкупа, база обязана туда же)
        out["_flow_end"] = max(d for d, _ in cfs)

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
# Максимум корпоратов (топ по ADV рынка) — bounds прогрев и пул Alor. Замер
# 13.10.2026: ликвидных корпоративных фиксов (оборот ≥1 млн ₽ или принт за 5
# дней) 1954, кап 700 отрезал ранги 865–1773 — РЖД и А101 пропадали из витрины
# целыми выпусками. Все 1954 в пуле (13.10, кап 2500) дали на проде CPU 65–107%
# и память 906→931 МиБ без плато при лимите 1,46 ГиБ, поэтому 1500. По ADV
# рынка за 30 дней ненулевой оборот у ~1490 кандидатов из 1954, так что кап
# отрезает почти один хвост без торгов (порядок внутри него произвольный).
# Менять только по замеру памяти/CPU (FIXED_CORP_CAP в env).
_CORP_CAP = int(os.getenv("FIXED_CORP_CAP", "1500"))
_uni_mem: dict = {"ts": 0.0, "rows": None}
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
        # БЕССРОЧНАЯ: MOEX отдаёт MATDATE '0000-00-00'. Дальше по проекту «даты
        # нет» — это None (фронт: horizon.js прячет перп из окна срока и уводит в
        # конец сортировки; screener_core.years_left), а строка-нули проходила
        # как дата: «00.00.0000» в таблице и первое место в сортировке по сроку.
        mat = g(row, "MATDATE") or None
        perpetual = str(mat or "").startswith("0000")
        out.append({
            "isin": isin, "secid": g(row, "SECID"),
            "name": nm, "issuer": _issuer_of(nm),
            "maturity_date": None if perpetual else mat, "perpetual": perpetual,
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
    """Фиксированный купон любого номинала.

    Рубль остаётся стартовым отбором витрины, но валюта выбирается на фронте.
    Поэтому не вырезаем замещающие/валютные выпуски здесь: иначе фильтр валюты
    был бы декоративным. G/Z-спред к рублёвой КБД для них намеренно не считаем.
    """
    if row["isin"] in floaters:
        return False
    if (row.get("secid") or "").startswith("BYM"):
        return False  # РесБел (Белоруссия) — квазисуверен под санкциями, вне скоупа
    cp = row.get("coupon_pct")
    if cp is None or cp <= 0 or not (row.get("maturity_date") or row.get("perpetual")):
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
        async with moex_client() as client:
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
    #
    # Оборот для ранжирования — СРЕДНЕДНЕВНОЙ за месяц по дневным итогам биржи
    # (весь рынок), а не VALTODAY в момент пересборки: тот зависит от часа и
    # дня — РЖД 1Р-05R (выпуск 20 млрд, ~50 млн ₽/день) в 13:00 имел 56 тыс ₽
    # оборота и стоял 900-м, за капом, а витрина его «не видела». Сегодняшний
    # оборот остаётся вторым ключом: свежий выпуск без истории заходит по нему.
    try:
        from services.block_trades import market_adv_map
        adv = await asyncio.to_thread(market_adv_map, 30)
    except Exception as e:
        logger.warning("fixed universe: market_adv_map failed: %s", e)
        adv = {}
    ofz = [r for r in rows if r["cls"] == "ofz"]
    linked = [r for r in rows if r["cls"] == "corp" and r.get("linked")]
    corp = sorted((r for r in rows if r["cls"] == "corp" and not r.get("linked")),
                  key=lambda r: max(adv.get(r["isin"], 0.0), r.get("val_today") or 0.0),
                  reverse=True)[:_CORP_CAP]
    rows = ofz + linked + corp
    # EMITTER_ID для фильтра «все бумаги эмитента» в общем мониторе (карта всего
    # рынка, durable; сеть — раз в сутки или на свежий выпуск)
    from services import emitters
    await emitters.ensure([r["isin"] for r in rows if r["cls"] != "ofz"])
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


def _ruble_face(row: dict) -> bool:
    return (row.get("faceunit") or "RUB").upper() in ("", "RUB", "SUR", "RUR")


def _accrued_face_ccy(row: dict, full: dict, calc_date: date) -> Optional[float]:
    """НКД на дату поставки В ВАЛЮТЕ НОМИНАЛА — той же, что поток и цена в %.

    Рублёвая бумага — биржевой ACCRUEDINT как есть. Валютная — из графика
    купонов: универс собран с TQCB, где расчёты рублёвые, и ACCRUEDINT там в
    РУБЛЯХ (НорНикЗ26Д 29.09.2026: 1004.45 при номинале 1000 USD, в долларах
    11.93). Сложенный с ценой в % от валютного номинала, он раздувал dirty
    вдвое, и YTM уходил в −99 % (та же ловушка разобрана в
    market_data.fetch_moex_securities). None — НКД не из чего посчитать:
    честный прочерк лучше смеси валют."""
    if _ruble_face(row):
        return row.get("accrued") or 0.0
    # кортежи (start, end, value): accrued_estimate распаковывает периоды по
    # позициям, dict-строку bondization он не поймёт
    periods = sorted(((s, e, c.get("value")) for c in (full.get("coupons") or [])
                      if (s := _d(c.get("start"))) and (e := _d(c.get("end"))) and e > s),
                     key=lambda p: (p[0], p[1]))
    if not periods:
        return None
    settle = settle_date(calc_date)
    if settle < periods[0][0]:
        return 0.0                      # начисление ещё не началось
    a = accrued_at(periods, settle)
    if a is None:
        a = accrued_estimate(periods, settle)
    return float(a) if a is not None else None


def fixed_side_metrics(row: dict, full: dict, g_curve, calc_date: date,
                       prices, known: Dict[float, dict] = None,
                       ru_curve=None) -> Dict[float, dict]:
    """{цена: {'ytm','g_spread_bps','y_idx_bps'}} по произвольным ценам одной бумаги.

    Стороны стакана, средневзвес, VWAP-набор тикета — всё это одна и та же
    бумага по разным ценам: поток, номинал и НКД от цены не зависят, меняется
    только дисконтирование. Считаем ПРЯМЫМ пересчётом на каждой цене, а не
    линеаризацией от last: наклон честен только рядом с якорем, а уехавший
    якорь уводит за собой все производные разом (флоатеры, 27.08.2026).
    Повторы цен схлопываются — bid==last у неликвида обычное дело; known —
    уже посчитанные цены той же бумаги (цена сделки), чтобы не считать дважды."""
    out: Dict[float, dict] = dict(known or {})
    face = row.get("settle_face") or row.get("face")
    accrued = _accrued_face_ccy(row, full, calc_date)
    if accrued is None:
        return out
    # КБД — рублёвая: спред к ней у валютной бумаги не считаем (тот же guard,
    # что в compute_fixed_row, — здесь для стакана и набора тикета)
    if not _ruble_face(row):
        g_curve = ru_curve = None
    for px in prices:
        if px is None or px <= 0:
            continue
        key = round(float(px), 4)
        if key in out:
            continue
        m = _metrics_at_price(row, full, px, accrued, calc_date, g_curve, face, ru_curve)
        out[key] = {"ytm": m.get("ytm_pct"), "g_spread_bps": m.get("g_spread_bps"),
                    "y_idx_bps": m.get("yield_over_index_bps")}
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
    # Дата размещения — начало ПЕРВОГО купона в графике MOEX: у фиксов в реестре
    # инструментов лежит лишь часть универса (реестр — про флоатеры), а
    # bondization уже в руках у всех. Начало первого купона = дата размещения
    # (стаб первого периода начинается в день размещения, не в день выпуска).
    starts = sorted(c.get("start") for c in (full.get("coupons") or []) if c.get("start"))
    out["issue_date"] = row.get("issue_date") or (starts[0] if starts else None)
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
                      price_override: float = None, ru_curve=None) -> dict:
    """Полный набор метрик фикс-бумаги для строки таблицы: цена (last→prev),
    YTM/тек.доходность/g-спред/z-спред/Y-IDX/дюрация/convexity/DV01.

    price_override — чистая цена калькулятора карточки: считает все метрики под
    произвольную цену вместо рыночной (last→prev). НКД/поток не зависят от цены.
    ru_curve — RUONIA-кривая (база Y-IDX). Не передана — Y-IDX прочерком: его
    надо прокидывать из каждой точки расчёта, как у флоатеров."""
    if price_override is not None:
        px = price_override
        out = {"last": px, "prev": row.get("prev"), "price_stale": False}
    else:
        px = row.get("last") if row.get("last") is not None else row.get("prev")
        out = {"last": px, "prev": row.get("prev"),
               "price_stale": row.get("last") is None and row.get("prev") is not None}
    _static_flags(out, row, full, calc_date)
    # НКД в валюте номинала: у валютной бумаги биржевой — в рублях
    accrued = _accrued_face_ccy(row, full, calc_date)
    if px is None or not full.get("coupons") or accrued is None:
        return out
    # КБД ОФЗ — рублёвая кривая. Доходность/дюрация валютной бумаги валидны в
    # её номинале, но вычитать из них RUB-кривую и называть результат спредом
    # нельзя. Передаём None, чтобы G/Z остались честным прочерком.
    ruble_face = _ruble_face(row)
    curve = g_curve if ruble_face else None
    ru = ru_curve if ruble_face else None
    m = _metrics_at_price(
        row, full, px, accrued, calc_date, curve,
        # номинал НА ДАТУ ПОСТАВКИ: face — на сегодня, а Σ будущих траншей
        # считается от settle; транш в окне (calc, settle] давал ложный отказ.
        row.get("settle_face") or row.get("face"), ru,
    )
    out.update({
        "ytm": m.get("ytm_pct"), "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
        "convexity": m.get("convexity"), "dv01": m.get("dv01"),
        "g_spread_bps": m.get("g_spread_bps"), "dirty": m.get("dirty"),
        "yield_over_index_bps": m.get("yield_over_index_bps"),
        "index_yield_pct": m.get("index_yield_pct"),
        "put_date": m.get("put_date"), "coupon_assumed_from": m.get("coupon_assumed_from"),
        # номинал на дату поставки и НКД — из них считаются ДЕНЬГИ уровня стакана
        # (фильтр по объёму: qty × (номинал × цена% + НКД)); имена те же, что у
        # флоатеров, чтобы арифметика книги на фронте была одна на две витрины
        # Фильтр объёма задан в рублях. Для валютного номинала не подменяем
        # номинальные деньги рублями без FX-конверсии: такой фильтр выключит
        # строку, а не нарисует ложный объём.
        "face_value_rub": m.get("face_current") if ruble_face else None,
        "accrued_rub": row.get("accrued") if ruble_face else None,
    })
    # МЕТРИКИ ПО ДРУГИМ ЦЕНАМ той же бумаги: средневзвес дня и стороны стакана.
    # Средневзвес — база аналитики: last price это ОДНА сделка, в неликвиде
    # случайный тонкий принт, часто на закрытии. Свой тиковый средневзвес
    # впереди биржевого: WAPRICE из ISS отстаёт. Стороны стакана — то, по чему
    # реально торгуют. Каждый новый уровень считается прямым пересчётом; уже
    # встречавшийся точный уровень берётся из LRU (см. fixed_side_metrics).
    if price_override is None:
        wap = pick_wap(row)
        bid, ask = row.get("bid"), row.get("ask")
        out["wap_pct"] = wap
        out["bid"] = bid
        out["ask"] = ask
        sides = fixed_side_metrics(
            row, full, curve, calc_date, (wap, bid, ask),
            known={round(float(px), 4): {"ytm": m.get("ytm_pct"),
                                         "g_spread_bps": m.get("g_spread_bps"),
                                         "y_idx_bps": m.get("yield_over_index_bps")}},
            ru_curve=ru)
        for side in ("wap", "bid", "ask"):
            price = {"wap": wap, "bid": bid, "ask": ask}[side]
            m_side = (sides.get(round(float(price), 4)) if price else None) or {}
            out[f"g_spread_{side}_bps"] = m_side.get("g_spread_bps")
            out[f"ytm_{side}"] = m_side.get("ytm")
            # имена — как у флоатеров (y_idx_bid_bps…): общий контракт витрины
            out[f"y_idx_{side}_bps"] = m_side.get("y_idx_bps")

    # z-спред над КБД ОФЗ (дискретный, метод НРД) — по тем же потокам
    if ruble_face and g_curve is not None and getattr(g_curve, "ok", lambda: False)() and m.get("dirty"):
        try:
            out["z_spread_bps"] = _z_spread_at_price(
                row, full, px, accrued, calc_date, g_curve,
                row.get("settle_face") or row.get("face"), m["dirty"],
            )
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


async def compute_fixed_metrics_all(universe: List[dict], g_curve, calc_date: date,
                                    ru_curve=None) -> Dict[str, dict]:
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
            out[u["isin"]] = compute_fixed_row(u, f, g_curve, calc_date, ru_curve=ru_curve)
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
