import calendar as _cal
import logging
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

from core.forwards import DiscountCurve, add_months, yf_act365

# Configure basic logging for the valuation module
logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# 1) Входные структуры
# -------------------------------------------------------------------------

@dataclass
class BondRefData:
    """
    Reference data for a floater bond.
    base: "RUONIA" or "KEYRATE"
    """
    isin: str
    base: str  
    spread_issue_bps: int
    face_value: float
    accrued_rub: float
    maturity_date: date
    first_coupon_date: date
    coupons_per_year: int
    issue_date: Optional[date] = None
    coupon_period_days: Optional[int] = None


@dataclass
class MarketPrice:
    """
    Market quotes for a bond.
    clean_price_pct: Price in % of face value (e.g., 101.25)
    """
    clean_price_pct: float


@dataclass
class Cashflow:
    """
    Simulated cashflow modeled on forward rates.
    type: "COUPON" or "REDEMPTION"

    Метаданные периода (опциональны, для display-таблицы карточки; солверы их не
    читают — только pay_date/amount_rub/type): чтобы display-строки строились из
    ТОГО ЖЕ потока, что прайсинг (без ручного форка build_cashflow_from_moex).
    """
    pay_date: date
    amount_rub: float
    type: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    coupon_rate_pct: Optional[float] = None   # годовая ставка купона, %
    base_rate_pct: Optional[float] = None      # индекс-компонента, %
    spread_bps: int = 0


# -------------------------------------------------------------------------
# 2) Основные функции расчёта
# -------------------------------------------------------------------------

def dirty_price_rub(face_value: float, clean_price_pct: float, accrued_rub: float) -> float:
    """
    Переход от 'чистой' цены к 'грязной' (в рублях).
    """
    return face_value * (clean_price_pct / 100.0) + accrued_rub


def face_for_pricing(face_value: float, amorts: Optional[List[dict]], calc_date: date) -> float:
    """Номинал, от которого котируется цена при T+1: амортизация с датой в окне
    (calc_date, settle] достаётся ПРОДАВЦУ (ex-date), покупатель платит цену от
    остатка ПОСЛЕ неё. MOEX FACEVALUE до выплаты ещё полный → вычитаем сами.
    Наблюдалось (БалтЛизП10 накануне амортизации): без вычета dirty на 100₽ выше
    реальной → SM −1593 вместо ≈ +1084."""
    settle = settle_date(calc_date)
    cut = 0.0
    for a in amorts or []:
        d = _amort_date(a)
        if d and calc_date < d <= settle and a.get("value") is not None:
            cut += float(a["value"])
    # Полное погашение в окне → остаток 0 (не фолбэк на полный номинал: `or`
    # на 0.0 давал завышенный dirty; такие бумаги ловит MATURED-guard выше)
    return max(face_value - cut, 0.0)


def generate_coupon_dates(first_coupon_date: date, maturity_date: date, coupons_per_year: int) -> List[date]:
    """
    Генерирует сетку дат купонов строго по шагу (без бизнес-сдвигов).
    Обрезает даты строго по maturity_date (включительно).
    Если maturity_date не ложится ровно на сетку, она НЕ добавляется как фиктивный купон.

    first_coupon_date=None (нет ни issue, ни первого купона — бумага вне
    MOEX-справочника по ISIN, напр. ОФЗ-ПК с SECID SU29xxx) → якорим сетку на
    maturity и шагаем НАЗАД: погашение всегда совпадает с последним купоном.
    Раньше тут был TypeError ('<=' None и date) — весь valuation бумаги падал.
    """
    if maturity_date is None:
        return []
    step_months = 12 // coupons_per_year
    if first_coupon_date is None:
        back = []
        current = maturity_date
        for _ in range(12 * coupons_per_year + 1):   # бэкстоп ~12 лет истории
            back.append(current)
            current = add_months(current, -step_months)
        return list(reversed(back))
    dates = []

    current = first_coupon_date
    while current <= maturity_date:
        dates.append(current)
        current = add_months(current, step_months)

    return dates


def generate_coupon_dates_by_period(issue_date: date, maturity_date: date, coupon_period_days: int) -> List[date]:
    if coupon_period_days <= 0:
        return []

    dates = []
    current = issue_date
    while True:
        current = current + timedelta(days=coupon_period_days)
        if current > maturity_date:
            break
        dates.append(current)

    if not dates or dates[-1] != maturity_date:
        dates.append(maturity_date)

    return dates


def _calib_coupons(periods) -> List[dict]:
    """Периоды [(start, end, value?),...] → dicts {start,end,value} для
    калибратора формулы купона (coupon_calib ест и date, и ISO-строки)."""
    out = []
    for p in periods or []:
        out.append({"start": p[0], "end": p[1], "value": p[2] if len(p) > 2 else None})
    return out


def extend_periods_to_maturity(periods, maturity: Optional[date]):
    """Достраивает недостающие купонные периоды от последнего известного до
    maturity (value=None → проекция форвардом). MOEX bondization отдаёт купоны
    страницами по 100; у длинных месячных бумаг (12 лет = 144 купона) расписание
    обрывается раньше погашения — без достройки поток теряет годы купонов, а
    голый номинал висит в пробеле → SM/z валятся в минус (МТС 003P-02: SM −74
    вместо +155). Достраиваем по медианному шагу имеющихся периодов.

    Принимает [(start, end, value?),...] (start/end — date или ISO-строка),
    возвращает [(date, date, value|None),...]. Идемпотентно (пробела нет → no-op)."""
    if not periods or maturity is None:
        return periods

    def _pd(x):
        if isinstance(x, date):
            return x
        try:
            return date.fromisoformat(x) if x else None
        except (ValueError, TypeError):
            return None

    norm = []
    for p in periods:
        s, e = _pd(p[0]), _pd(p[1])
        v = p[2] if len(p) > 2 else None
        if s and e:
            norm.append((s, e, v))
    if not norm:
        return periods
    norm.sort(key=lambda x: x[1])
    last_end = norm[-1][1]

    # медианный шаг (дни) — устойчив к нерегулярному первому/последнему периоду
    steps = sorted((e - s).days for s, e, _ in norm if (e - s).days > 0)
    step = steps[len(steps) // 2] if steps else 30
    if step <= 0:
        return norm

    # Достраиваем ТОЛЬКО при пробеле от полного периода (недостаёт ≥1 купон).
    # Мелкий зазор (last_end чуть раньше maturity из-за day-count/выходных или
    # T+n конвенции даты погашения) — последний купон уже = погашение, no-op.
    if (maturity - last_end).days < step * 0.5:
        return norm

    cur = last_end
    guard = 0
    while cur < maturity and guard < 600:
        guard += 1
        nxt = cur + timedelta(days=step)
        if nxt >= maturity:
            norm.append((cur, maturity, None))
            break
        norm.append((cur, nxt, None))
        cur = nxt
    return norm


def _amort_date(a) -> Optional[date]:
    """Парсит дату амортизации (ISO-строка из MOEX bondization) → date | None."""
    d = a.get("date")
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(d) if d else None
    except (ValueError, TypeError):
        return None


# Нерабочие дни MOEX (расчёты НКЦ): фиксированные федеральные праздники.
# MOEX торгует большинство «мостов» и новогодних (3–6, 8 янв) — в списке только
# дни, когда расчётов нет. Переносы правительства на мосты — через оверрайд
# data/moex_holidays.json: {"extra_dates": ["YYYY-MM-DD", ...], "trading_dates":
# ["YYYY-MM-DD", ...]} (extra — нерабочие сверх базовых, trading — рабочие
# вопреки базовым, напр. перенесённая рабочая суббота). Файл опционален,
# обновляется по календарю MOEX (https://www.moex.com/s371) без правки кода.
#
# 31 декабря — нерабочий с 2023 (перенос выходного): последний день расчётов в
# году — 30 декабря, дальше поставка уезжает на первый рабочий день января.
# Без него settle с 30.12 падал на 31.12 и сдвигал НКД/дисконт в конце года.
# Годы, когда 31.12 ещё торговали (до 2023), правятся через trading_dates —
# as-of расчёты на такие даты в проде не ведутся (архив с 2026-07-30).
_MOEX_HOLIDAYS_MD = {(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
                     (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4), (12, 31)}


def _load_holiday_overrides():
    import os
    import json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "moex_holidays.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        extra = {date.fromisoformat(s) for s in raw.get("extra_dates", [])}
        trading = {date.fromisoformat(s) for s in raw.get("trading_dates", [])}
        return extra, trading
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
        return set(), set()


_HOLIDAY_EXTRA, _HOLIDAY_TRADING = _load_holiday_overrides()


def _is_settlement_day_off(d: date) -> bool:
    if d in _HOLIDAY_TRADING:
        return False
    if d in _HOLIDAY_EXTRA:
        return True
    return d.weekday() >= 5 or (d.month, d.day) in _MOEX_HOLIDAYS_MD


def settle_date(calc_date: date) -> date:
    """Дата расчётов T+1 (скип выходных И праздников MOEX). Купон с pay_date <= settle
    покупателю НЕ достаётся (MOEX НКД уже обнулён под эту конвенцию) — включать его в
    cashflow нельзя: накануне выплаты PV завышается на целый купон
    (наблюдалось: DM +530bps мусора в день перед купоном; тот же эффект давали
    праздники — например, 30 декабря T+1 попадал на 1 января)."""
    d = calc_date + timedelta(days=1)
    while _is_settlement_day_off(d):
        d += timedelta(days=1)
    return d


def _period_bounds(p) -> tuple:
    """(start, end, value) из элемента расписания: кортеж (s,e,v) ИЛИ dict."""
    try:
        if isinstance(p, (tuple, list)):
            s, e, v = p[0], p[1], (p[2] if len(p) > 2 else None)
        else:
            s, e, v = p.get("start"), p.get("end"), p.get("value")
        if isinstance(s, str):
            s = date.fromisoformat(s[:10])
        if isinstance(e, str):
            e = date.fromisoformat(e[:10])
        return s, e, (float(v) if v is not None else None)
    except Exception:
        return None, None, None


def period_at(periods, d: date) -> Optional[tuple]:
    """Купонный период (start, end, value), в который попадает d: start ≤ d < end."""
    for p in periods or []:
        s, e, v = _period_bounds(p)
        if s and e and e > s and s <= d < e:
            return (s, e, v)
    return None


def accrued_at(periods, d: date) -> Optional[float]:
    """НКД на дату d линейно внутри купонного периода: value·(d−start)/(end−start).
    None, если период не найден или купон ещё не опубликован (value=None)."""
    p = period_at(periods, d)
    if not p or p[2] is None:
        return None
    s, e, v = p
    return v * (d - s).days / ((e - s).days or 1)


def accrue_to_settle(accrued_calc: Optional[float], calc_date: date,
                     periods) -> tuple[Optional[float], Optional[str]]:
    """НКД на ДАТУ ПОСТАВКИ из НКД на calc_date. Возвращает (нкд, пояснение|None).

    Зачем: деньги за бумагу уходят на settle (T+1 раб), и вся модель уже
    дисконтирует оттуда — XIRR, simple margin, discount margin якорятся на
    settle_date(). А НКД до этого брался на calc_date, т.е. dirty был занижен
    на 1–3 дня накопления (при купоне 22% годовых это ≈6 b.p. цены за день и
    единицы b.p. в SM/DM). Теперь обе стороны сравнения — на одну дату.

    Три случая:
      • период тот же, купон опубликован → факт + value·Δдней/длина периода;
      • период тот же, купон не опубликован → пропорция по дням от самого факта;
      • период сменился (в окне (calc, settle] выплата) → купон уходит продавцу,
        НКД на settle = чистое начисление НОВОГО периода из графика.
    """
    if accrued_calc is None:
        return None, None
    settle = settle_date(calc_date)
    gap = (settle - calc_date).days
    if gap <= 0 or not periods:
        return accrued_calc, None

    p_c, p_s = period_at(periods, calc_date), period_at(periods, settle)
    note = f"НКД доначислен с {calc_date.isoformat()} до даты поставки {settle.isoformat()} ({gap} дн)"

    if p_c and p_s and p_c[0] == p_s[0]:
        s, e, v = p_s
        if v is not None:
            daily = v / ((e - s).days or 1)
            return round(accrued_calc + daily * gap, 4), note
        elapsed = (calc_date - s).days
        if elapsed > 0:
            return round(accrued_calc * (settle - s).days / elapsed, 4), note
        return accrued_calc, None

    a = accrued_at(periods, settle)
    if a is None and p_s is not None:
        # Купон НОВОГО периода ещё не опубликован (value=None) — accrued_at даёт
        # None, и фолбэк «оставить старый НКД» тут ловушка: cashflow уже отдал
        # купон продавцу (pay_date <= settle), а dirty оставался с ПОЛНЫМ старым
        # НКД → завышен на целый купон → guard номинального убытка гасил
        # SM/DM/y_idx в день перед выплатой (Магнит5Р03 @ 2026-07-27).
        s2, _e2, _v2 = p_s
        k = (settle - s2).days
        if k <= 0:
            a = 0.0          # поставка в самый старт периода — начислять нечего
        elif p_c is not None:
            # k дней нового периода по дневной ставке СТАРОГО купона — лучшая
            # оценка, пока новый купон не опубликован
            sc, ec, vc = p_c
            if vc is not None:
                a = round(vc / ((ec - sc).days or 1) * k, 4)
            elif (calc_date - sc).days > 0:
                a = round(accrued_calc / (calc_date - sc).days * k, 4)
    if a is not None:
        return round(a, 4), (f"ex-coupon: купон уходит продавцу (поставка {settle.isoformat()}), "
                             "НКД пересчитан от начала нового купонного периода")
    return accrued_calc, None


def offer_kind(type_str: Optional[str]) -> str:
    """Тип оферты MOEX offertype → {'call','put'}.

    'call' — опцион ЭМИТЕНТА (досрочное погашение по его усмотрению): держатель
    НЕ может форсировать выкуп, поэтому yield-to-call — не его гарантированный
    исход (сработает лишь если эмитенту выгодно, обычно на премии). 'put' —
    безотзывная оферта ДЕРЖАТЕЛЯ (может предъявить к выкупу) — его пол/floor.

    Эвристика по тексту; при неоднозначности → 'put' (конвенция RU-рынка:
    «оферта» = пут держателя). Call помечаем ТОЛЬКО по явным маркерам опциона
    эмитента, чтобы не переклассифицировать обычные путы."""
    t = (type_str or "").lower()
    if ("call" in t or "колл" in t or "опцион эмитент" in t
            or "усмотрени" in t and "эмитент" in t):
        return "call"
    return "put"


def next_offer_info(offers: Optional[List[dict]],
                    settle: date,
                    maturity: Optional[date] = None) -> Optional[tuple]:
    """Ближайшая БУДУЩАЯ оферта любого вида из MOEX bondization offers:
    (date, type_str, kind∈{'put','call'}) или None. Состоявшиеся/исполненные
    отфильтрованы — не будущее событие, даже если дата распарсилась в будущее.
    В отличие от first_offer_date (горизонт оценки, только путы) — это
    информационный флаг «у бумаги есть оферта», call тоже показываем.

    maturity — дата погашения: записи НА НЕЁ И ПОЗЖЕ отбрасываются. У части
    выпусков (ВЭБP-42) bondization содержит единственную строку «Оферта/
    Погашение» с датой погашения — это техническая запись самого погашения, а
    не опцион: держателю нечего предъявлять, альтернативного горизонта нет.
    Без фильтра карточка рисовала «Оферта (пут)», таблица — маркер p, а
    свитчер горизонта не появлялся (и правильно) — UI противоречил сам себе."""
    best = None
    for o in offers or []:
        typ = (o.get("type") or "").lower()
        if "состоя" in typ or "исполн" in typ:
            continue
        d = o.get("date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        if maturity is not None and isinstance(d, date) and d >= maturity:
            continue                      # запись на дату погашения = само погашение
        if isinstance(d, date) and d > settle and (best is None or d < best[0]):
            best = (d, o.get("type"), offer_kind(typ))
    return best


def first_offer_date(offers: Optional[List[dict]], settle: date) -> Optional[date]:
    """Первая будущая оферта-ПУТ из MOEX bondization offers [{date,type,price},...].
    Для флоатера купонные value после оферты в bondization не определены и спред
    может быть пересмотрен — потоки за офертой фикция. Оцениваем к оферте
    (yield-to-put, как НРД): купоны до неё + выкуп остатка номинала.

    Учитываем ТОЛЬКО путы: call (опцион эмитента) держатель не форсирует, поэтому
    он не задаёт горизонт «худшего» для держателя (см. offer_kind)."""
    dates = []
    for o in offers or []:
        # уже состоявшаяся оферта («Оферта (состоялось)») — не будущее событие,
        # не должна двигать горизонт оценки, даже если дата распарсилась в будущее
        typ = (o.get("type") or "").lower()
        if "состоя" in typ or "исполн" in typ:
            continue
        if offer_kind(typ) == "call":       # опцион эмитента — не горизонт держателя
            continue
        d = o.get("date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        if isinstance(d, date) and d > settle:
            dates.append(d)
    return min(dates) if dates else None


def first_call_date(offers: Optional[List[dict]], settle: date) -> Optional[date]:
    """Первая будущая CALL-оферта (опцион эмитента) из MOEX bondization offers.

    Зеркало first_offer_date: там горизонт держателя (пут), здесь — горизонт
    ЭМИТЕНТА. Держатель call не форсирует, поэтому это не «худший» исход, а
    ВЕРОЯТНЫЙ: эмитент выкупит, если занял дорого (цена бумаги выше цены выкупа
    — правило колла, зеркальное правилу пута). Выбор горизонта по цене живёт в
    services/valuation.preferred_horizon."""
    dates = []
    for o in offers or []:
        typ = (o.get("type") or "").lower()
        if "состоя" in typ or "исполн" in typ:
            continue
        if offer_kind(typ) != "call":
            continue
        d = o.get("date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        if isinstance(d, date) and d > settle:
            dates.append(d)
    return min(dates) if dates else None


def offer_price_pct(offers: Optional[List[dict]], offer_date: date) -> float:
    """Публичное имя _offer_price_pct (цена выкупа на дате оферты, % от номинала)."""
    return _offer_price_pct(offers, offer_date)


def _offer_price_pct(offers: Optional[List[dict]], offer_date: date) -> float:
    """Цена выкупа на оферте в % (обычно 100; MOEX может дать явную)."""
    for o in offers or []:
        d = o.get("date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        if d == offer_date and o.get("price") is not None:
            try:
                return float(o["price"])
            except (ValueError, TypeError):
                pass
    return 100.0


def _check_curve_convention(base: str, curve) -> None:
    """Кривая обязана отдавать forward() в конвенции базы бумаги:
    RUONIA → daily_comp, KEYRATE → simple; 'level' (плоский индекс) допустим для
    обеих (DM-путь). Иначе начисление/дисконт компаундят дважды или недокомпаундят
    — раньше контракт был неявным (по типу кривой) и ничем не проверялся."""
    conv = getattr(curve, "rate_convention", None)
    if conv is None or conv == "level":
        return
    expected = "daily_comp" if base == "RUONIA" else "simple"
    if conv != expected:
        raise ValueError(
            f"rate_convention кривой '{conv}' не соответствует базе {base} "
            f"(ожидается '{expected}' или 'level')")


def build_cashflows_to_maturity(
    bond: BondRefData,
    curve: DiscountCurve,
    calc_date: date,
    explicit_periods: Optional[List[tuple]] = None,
    amorts: Optional[List[dict]] = None,
    offers: Optional[List[dict]] = None,
    to_offer: bool = False,
    cut_date: Optional[date] = None,
    index_pct_fn=None,
    warnings_out: Optional[list] = None,
) -> List[Cashflow]:
    """
    Строит ожидаемые cashflows: купоны и погашение принципала.
    Участвуют только CF с pay_date > calc_date.

    explicit_periods — реальное расписание из MOEX (приоритет), тройки
    [(start, end, value|None),...]; value — зафиксированная рублёвая сумма купона
    (bondization). Если value задан — купон берётся ФАКТОМ (не перепрогнозируется
    форвардом): текущий/прошлый купон флоатера фиксируется на старте периода и
    известен (та же логика, что в services/zspread.project_cfs). Пары (start,end)
    тоже принимаются (value=None). Иначе — генерация по issue+period_days /
    first_coupon (приближение, может дрейфовать).

    amorts — график амортизаций MOEX [{date, value},...]. Если есть погашение
    принципала ДО maturity (амортизируемая бумага), принципал платится по датам
    амортизаций, а прогнозные купоны начисляются от ОСТАТОЧНОГО номинала (сумма
    амортизаций после начала периода). Иначе — bullet (номинал целиком на maturity).

    index_pct_fn — инжектированный провайдер индекса начавшегося периода с
    сигнатурой period_index_pct (service-слой биндит в него историю ЦБ, см.
    coupon_calib.index_history) — ядро само в сеть не ходит. None → легаси-фолбэк
    на lazy-импорт (CLI-пути). warnings_out — аккумулятор деградаций: сбой
    провайдера больше не глотается молча, а помечается (купон уходит на форвард).
    """
    _check_curve_convention(bond.base, curve)
    if explicit_periods:
        # Реальный календарь MOEX: тройки (start, end, value) или пары (start, end).
        # maturity_date может быть None (перпы/суборды без даты) — не фильтруем.
        # Достраиваем хвост до maturity: bondization обрывается на 100-м купоне
        # (пагинация ISS), у длинных бумаг пробел купоны→погашение = годы.
        ep = extend_periods_to_maturity(explicit_periods, bond.maturity_date)
        periods = []
        for p in ep:
            s, e = p[0], p[1]
            v = p[2] if len(p) > 2 else None
            if bond.maturity_date is None or e <= bond.maturity_date:
                periods.append((s, e, v))
    else:
        if bond.coupon_period_days and bond.issue_date:
            coupon_dates = generate_coupon_dates_by_period(bond.issue_date, bond.maturity_date, bond.coupon_period_days)
            start_1 = bond.issue_date
        else:
            coupon_dates = generate_coupon_dates(bond.first_coupon_date, bond.maturity_date, bond.coupons_per_year)
            step_months = 12 // bond.coupons_per_year
            # first_coupon неизвестен → сетка заякорена на maturity (см.
            # generate_coupon_dates), start_1 — шаг назад от её первой даты
            anchor = bond.first_coupon_date or (coupon_dates[0] if coupon_dates else None)
            if anchor is None:
                raise ValueError(f"no maturity/first_coupon to build schedule for {bond.isin}")
            start_1 = add_months(anchor, -step_months)
            if bond.issue_date and start_1 < bond.issue_date:
                start_1 = bond.issue_date

        # Validation
        if any(d > bond.maturity_date for d in coupon_dates):
            raise ValueError(f"Found coupon pay_date after maturity_date for ISIN: {bond.isin}")

        # 2. rebuild periods (генерация — value неизвестен, всегда прогноз)
        periods = []
        prev_end = start_1
        for d in coupon_dates:
            periods.append((prev_end, d, None))
            prev_end = d

    # T+1: платежи с датой <= settle покупателю не достаются (ex-coupon, НКД=0)
    settle = settle_date(calc_date)

    # Оферта. Три горизонта:
    #  cut_date задан — режем РОВНО к этой дате (явный выбор горизонта: пут, call
    #                   или ручной свитчер карточки). Приоритет над to_offer.
    #  to_offer=True  — yield-to-put: режем к ПЕРВОЙ будущей оферте безусловно
    #                   (выкуп остатка номинала по цене оферты). Для бумаг с
    #                   офертой это первостепенная цифра — рынок торгует к оферте.
    #  to_offer=False — к погашению; режем ТОЛЬКО если купон после оферты неопределён
    #                   (пересмотр эмитентом — ref_data.cut_at_offer). Сохранено для
    #                   сверки с НРД (НРД считает спреды к погашению).
    put = None
    if cut_date is not None:
        put = cut_date
    elif offers:
        try:
            if to_offer:
                put = first_offer_date(offers, settle)
            else:
                from services.ref_data import cut_at_offer
                if cut_at_offer(bond.isin):
                    put = first_offer_date(offers, settle)
        except Exception:
            put = None
    eff_maturity = bond.maturity_date
    if put and (eff_maturity is None or put < eff_maturity):
        eff_maturity = put
    else:
        put = None  # оферта не раньше погашения — обычный путь

    # Оферта внутри купонного периода: страддлящий период режем в стаб
    # [start, put] с пропорциональным купоном (зафикс. value — по доле дней,
    # прогнозный — проекция на укороченный период). Раньше купон выбрасывался
    # целиком — PV к оферте занижался на частичный НКД.
    if put:
        adj = []
        for (s, e, v) in periods:
            if s < put < e:
                ratio = (put - s).days / ((e - s).days or 1)
                adj.append((s, put, float(v) * ratio if v is not None else None))
            else:
                adj.append((s, e, v))
        periods = adj

    # Амортизации: будущие погашения принципала (dates > settle, до горизонта).
    future_am = sorted(
        (d, float(a["value"]))
        for a in (amorts or [])
        if a.get("value") is not None and (d := _amort_date(a)) and d > settle
        and (eff_maturity is None or d <= eff_maturity)
    )
    amortizing = any(eff_maturity and d < eff_maturity for d, _ in future_am)

    # Номинал, от которого котируется цена при T+1 (амортизация в окне
    # (calc, settle] ушла продавцу). ВСЯ выплата принципала ниже сводится именно
    # к нему, иначе поток и цена считаются от разных баз. Раньше bullet-ветка
    # платила сырой bond.face_value: MOEX FACEVALUE до выплаты ещё полный, и при
    # конфигурации «транш в окне + финальный на maturity» флаг amortizing
    # оказывался False → погашение завышалось на сумму транша, уже не
    # причитающегося покупателю.
    pricing_face = face_for_pricing(bond.face_value, amorts, calc_date)

    # Кэп/флор купона (MIN(КС+m;X%)/«не более», MAX/«не менее») — потолок/пол ставки
    # годовых. Прайсим прогнозные купоны с клэмпом (зафикс. value уже с кэпом фактом).
    cap_pct = floor_pct = None
    margin_steps = ref_margin_bps = None
    if bond.base in ("RUONIA", "KEYRATE"):
        try:
            from services.ref_data import coupon_formula as _cf
            _sp = _cf(bond.isin)
            cap_pct, floor_pct = _sp.get("cap_pct"), _sp.get("floor_pct")
            # Маржа-лесенка: надбавка меняется по номерам купонов («S 1-7 = 2.5%,
            # S8-21 = 4.6%»). Применяем ДЕЛЬТОЙ к spread_issue_bps относительно
            # скаляра реестра (margin_bps): солверы SM/DM варьируют spread_bps —
            # абсолютная подмена ломала бы их линейность. Ординал купона осмыслен
            # только на реальном календаре MOEX (explicit_periods): генерённая
            # сетка может стартовать не с 1-го купона.
            if explicit_periods:
                margin_steps = _sp.get("margin_schedule")
                ref_margin_bps = _sp.get("margin_bps")
        except Exception:
            cap_pct = floor_pct = None

    cfs = []

    # 3. generate coupons
    for _ordinal0, (start, end, value) in enumerate(periods):
        if end <= settle:
            continue
        # эффективный спред периода: скаляр выпуска + дельта лесенки маржи
        # (bps диапазона − скаляр реестра) для купона № _ordinal0+1
        sp_bps = bond.spread_issue_bps
        if margin_steps:
            for _st in margin_steps:
                if _st["from"] <= _ordinal0 + 1 <= _st["to"]:
                    sp_bps = bond.spread_issue_bps + _st["bps"] - (ref_margin_bps or 0)
                    break
        if eff_maturity and end > eff_maturity:
            continue

        # #3: для амортизируемых начисляем от остаточного номинала на начало
        # периода = текущий номинал − амортизации до start (формула инвариантна
        # к обрезке future_am на оферте, в отличие от Σ будущих после start)
        face = pricing_face
        if amortizing:
            paid_by_start = sum(v for d, v in future_am if d <= start)
            face = max(pricing_face - paid_by_start, 0.0)
        # Купон за ПОЛНЫЙ период [start, end]; days по полному периоду.
        days = (end - start).days
        alpha = days / 365.0
        base_pct = None   # индекс-компонента (для display); зафикс. купон — None

        if value is not None:
            # #1: зафиксированный купон — факт MOEX, без перепрогноза форвардом
            coupon_amt = float(value)
        else:
            # Форвард-ставку для прошлого стаба клэмпим к calc_date: кривая
            # стартует с calc_date+1 и за прошлые даты осмысленной ставки не даёт.
            # Начавшийся период (start ≤ calc): индекс уже (частично) определён по
            # факту — спека формулы выпуска (point/average+лаг, manual > калибратор,
            # та же логика, что display в services/cashflow) с фолбэком на точечный
            # фиксинг КС на start. Иначе — проекция форвардом.
            #
            # ЕДИНАЯ МЕТОДИКА (решение 2026-07-31): ВСЕ непрогнозные и прогнозные
            # купоны с известной спекой считаются по букве проспекта — окно
            # наблюдения с лагом, факт ЦБ + форвард-ступени кривой, simple
            # начисление (то же, что паспорт «Фиксинг по дням»). Одна цифра
            # купона во всех вкладках. Цена решения: у RUONIA-бумаг SM на паре
            # может отходить от маржи на лаг/наклон кривой (бывшее par-тождество
            # daily-comp) — осознанный трейд-офф в пользу верифицированной
            # методики. Daily-comp фактор кривой (else-ветка ниже) остался
            # ФОЛБЭКОМ для бумаг без спеки (period_index_pct → None).
            idx_pct = None
            if bond.base in ("RUONIA", "KEYRATE"):
                try:
                    fn = index_pct_fn
                    if fn is None:                     # легаси-фолбэк (CLI-пути)
                        from services.coupon_calib import period_index_pct as fn
                    # по-дневная проекция: ступень сегмента кривой (daily_forward),
                    # НЕ окно до конца периода — то интерполировало ставку между
                    # тенорами и размазывало скачки (см. forwards.daily_forward)
                    fwd_pct = lambda dt: curve.daily_forward(dt) * 100.0
                    # face = ТЕКУЩИЙ остаток (не pricing_face): калибратор сам
                    # откатывает номинал назад по amorts, а транш из окна
                    # (calc, settle] в прошлых периодах ещё не был выплачен.
                    idx_pct = fn(bond.isin, bond.base, _calib_coupons(periods),
                                 bond.face_value, start, end, calc_date, fwd_pct,
                                 amorts=amorts)
                except Exception as e:
                    idx_pct = None
                    if warnings_out is not None:
                        warnings_out.append(
                            f"фиксинг периода {start}: {type(e).__name__} — купон спроецирован форвардом")
            if idx_pct is not None:
                # конвенция выпуска: (индекс + маржа) simple ACT/365
                factor = (idx_pct / 100.0 + sp_bps / 10000.0) * alpha
                base_pct = idx_pct
            else:
                f_start = start if start > calc_date else calc_date
                f_rate = curve.forward(f_start, end) if f_start < end else 0.0
                sp = sp_bps / 10000.0
                if bond.base == "RUONIA":
                    # индекс капитализируется дневно (daily-comp форвард воспроизводит
                    # factor кривой), маржа — simple, как в конвенции выпусков
                    factor = (1.0 + f_rate / 365.0)**days - 1.0 + sp * alpha
                elif bond.base == "KEYRATE":
                    # конвенция выпуска: (КС + маржа) simple ACT/365; форвард кривой
                    # quarterly-nominal ≈ уровень КС (как zspread.project_cfs)
                    factor = (f_rate + sp) * alpha
                else:
                    raise ValueError(f"Unknown base rate type: {bond.base} for ISIN: {bond.isin}")
                base_pct = f_rate * 100.0

            # Кэп/флор: клэмп ГОДОВОЙ ставки купона (factor/alpha) в [floor, cap],
            # затем обратно в factor. Работает для обеих конвенций (RUONIA daily-comp
            # и KEYRATE simple), т.к. годовая ставка = coupon/face·365/days = factor/alpha.
            if (cap_pct is not None or floor_pct is not None) and alpha > 0:
                ann = factor / alpha * 100.0
                clamped = ann
                if cap_pct is not None:
                    clamped = min(clamped, cap_pct)
                if floor_pct is not None:
                    clamped = max(clamped, floor_pct)
                if clamped != ann:
                    factor = clamped / 100.0 * alpha

            coupon_amt = face * factor

        # ставка купона годовых (для display-таблицы): coupon/face·365/days
        cpn_rate = (coupon_amt / face * 365.0 / days * 100.0) if (face > 0 and days > 0) else None
        cfs.append(Cashflow(
            pay_date=end, amount_rub=coupon_amt, type="COUPON",
            period_start=start, period_end=end,
            coupon_rate_pct=round(cpn_rate, 4) if cpn_rate is not None else None,
            base_rate_pct=round(base_pct, 4) if base_pct is not None else None,
            spread_bps=sp_bps or 0,
        ))

    # 4. Выплата принципала. Единая схема вместо трёх веток: сначала все будущие
    # амортизации по графику, затем НЕПОГАШЕННЫЙ ОСТАТОК одним платежом — на дату
    # оферты (по её цене) либо на погашение. Инвариант: Σ принципала == pricing_face
    # ровно, при любой комбинации (bullet / амортизируемая / оферта / T+1 окно).
    for d, v in future_am:
        cfs.append(Cashflow(pay_date=d, amount_rub=v, type="REDEMPTION",
                            period_start=d, period_end=d))
    residual = pricing_face - sum(v for _d, v in future_am)
    if residual > 1e-9:
        if put:
            px = _offer_price_pct(offers, put) / 100.0
            cfs.append(Cashflow(pay_date=put, amount_rub=residual * px, type="REDEMPTION",
                                period_start=put, period_end=put))
        elif bond.maturity_date and bond.maturity_date > calc_date:
            cfs.append(Cashflow(pay_date=bond.maturity_date, amount_rub=residual, type="REDEMPTION",
                                period_start=bond.maturity_date, period_end=bond.maturity_date))

    # 5. Sort by pay_date
    cfs.sort(key=lambda cf: cf.pay_date)
    return cfs


def build_cashflows_with_spread(
    bond: BondRefData,
    curve: DiscountCurve,
    calc_date: date,
    spread_bps: int,
    explicit_periods: Optional[List[tuple]] = None,
    amorts: Optional[List[dict]] = None,
    offers: Optional[List[dict]] = None,
    to_offer: bool = False,
    cut_date: Optional[date] = None,
    index_pct_fn=None,
    warnings_out: Optional[list] = None,
) -> List[Cashflow]:
    bond_variant = BondRefData(
        isin=bond.isin,
        base=bond.base,
        spread_issue_bps=spread_bps,
        face_value=bond.face_value,
        accrued_rub=bond.accrued_rub,
        maturity_date=bond.maturity_date,
        first_coupon_date=bond.first_coupon_date,
        coupons_per_year=bond.coupons_per_year,
        issue_date=bond.issue_date,
        coupon_period_days=bond.coupon_period_days,
    )
    return build_cashflows_to_maturity(bond_variant, curve, calc_date,
                                       explicit_periods=explicit_periods, amorts=amorts,
                                       offers=offers, to_offer=to_offer, cut_date=cut_date,
                                       index_pct_fn=index_pct_fn, warnings_out=warnings_out)


def xnpv(rate: float, cashflows: List[tuple[date, float]]) -> float:
    if rate <= -1.0:
        raise ValueError("rate must be > -1")
    if not cashflows:
        return 0.0

    d0 = cashflows[0][0]
    total = 0.0
    for d, amount in cashflows:
        years = (d - d0).days / 365.0
        total += amount / math.pow(1.0 + rate, years)
    return total


def xirr(cashflows: List[tuple[date, float]], low: float = -0.9999, high: float = 10.0, tol: float = 1e-10) -> Optional[float]:
    if not cashflows:
        return None

    amounts = [amount for _, amount in cashflows]
    if not any(amount < 0 for amount in amounts) or not any(amount > 0 for amount in amounts):
        return None

    def f(rate: float) -> float:
        return xnpv(rate, cashflows)

    f_low = f(low)
    f_high = f(high)

    expand_count = 0
    while f_low * f_high > 0 and expand_count < 20:
        high *= 2.0
        f_high = f(high)
        expand_count += 1

    if f_low * f_high > 0:
        return None

    for _ in range(200):
        mid = (low + high) / 2.0
        f_mid = f(mid)

        if abs(f_mid) < tol or abs(high - low) < tol:
            return mid

        if f_low * f_mid <= 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid

    return (low + high) / 2.0


def xirr_yield_pct(dirty_price_rub: float, cashflows: List[Cashflow], calc_date: date) -> Optional[float]:
    # ЯКОРЬ = ДАТА ПОСТАВКИ (T+1 раб; пятница → понедельник): деньги за бумагу
    # реально уходят на settle, доходность считается от неё, не от calc_date
    settle = settle_date(calc_date)
    dated_amounts = [(settle, -dirty_price_rub)]
    dated_amounts.extend((cf.pay_date, cf.amount_rub) for cf in cashflows if cf.pay_date > settle)
    rate = xirr(dated_amounts)
    if rate is None:
        return None
    return rate * 100.0


def pv_cashflows_with_dm(
    bond: BondRefData, 
    curve: DiscountCurve, 
    cashflows: List[Cashflow], 
    calc_date: date, 
    dm_bps: int
) -> float:
    """
    Считает PV всех cashflows рекурсивным дисконтированием: F_i + DM на сетке платежей.

    Конвенция дисконта зеркалит конвенцию начисления купона (build_cashflows):
      RUONIA  — период-фактор (1+f/365)^days + dm·days/365 (индекс компаундится,
                маржа simple);
      KEYRATE — 1 + (f+dm)·days/365 (money-market simple).
    Так поток «(индекс+маржа) simple + номинал» телескопируется точно: цена =
    номинал ⇔ SM = маржа выпуска.
    """
    _check_curve_convention(bond.base, curve)
    dm = dm_bps / 10000.0

    # Якорь дисконта = дата поставки (T+1 раб): PV сравнивается с dirty,
    # который платится на settle, — дисконтируем платежи к settle
    settle = settle_date(calc_date)
    pay_dates_set = {cf.pay_date for cf in cashflows if cf.pay_date > settle}
    grid = sorted(list(pay_dates_set))

    df_dm = 1.0
    prev = settle
    pv = 0.0

    for d in grid:
        days = (d - prev).days
        alpha = days / 365.0

        f_rate = curve.forward(prev, d)

        if bond.base == "RUONIA":
            base_factor = (1.0 + f_rate / 365.0)**days + dm * alpha
            if base_factor <= 0.0:
                raise ValueError("Rate too negative")
            df_dm /= base_factor
        elif bond.base == "KEYRATE":
            base_factor = 1.0 + (f_rate + dm) * alpha
            if base_factor <= 0.0:
                raise ValueError("Rate too negative")
            df_dm /= base_factor
        else:
            raise ValueError(f"Unknown base: {bond.base}")
            
        # Сумма всех CF в эту дату (может быть и купон, и погашение)
        amount_on_d = sum(cf.amount_rub for cf in cashflows if cf.pay_date == d)
        pv += amount_on_d * df_dm
        
        prev = d
        
    return pv


def solve_simple_margin_bps(
    bond: BondRefData,
    curve: DiscountCurve,
    cashflows: List[Cashflow],
    calc_date: date,
    dirty_target_rub: float,
    low_bps: int = -50000,
    high_bps: int = 50000,
    tol_bps: int = 1
) -> Optional[int]:
    """SIMPLE MARGIN (наш sm_bps): бисекция маржи, приравнивающей PV к dirty.
    Дисконт по ФОРВАРД-кривой базы в конвенции начисления купона (pv_cashflows_with_dm).
    НЕ путать с discount margin: настоящий FRN DM — solve_discount_margin_bps
    (плоский индекс + money-market). Имя solve_dm_bps сохранено как алиас ниже
    (легаси: раньше это поле звалось dm_bps, потом добавили настоящий DM).
    """
    def f(dm_bps_val: int) -> float:
        try:
            return pv_cashflows_with_dm(bond, curve, cashflows, calc_date, dm_bps_val) - dirty_target_rub
        except ValueError:
            return float('inf') if dm_bps_val < 0 else -float('inf')
        
    f_low = f(low_bps)
    f_high = f(high_bps)
    
    if f_low * f_high > 0:
        logger.warning(
            f"Невозможно забрекетировать DM для {bond.isin}: f({low_bps}bps)={f_low:,.2f}, f({high_bps}bps)={f_high:,.2f}"
        )
        return None
        
    low = low_bps
    high = high_bps
    
    while (high - low) > tol_bps:
        mid = (low + high) // 2
        f_mid = f(mid)
        
        if abs(f_mid) < 1e-8:
            return mid
            
        if f_low * f_mid < 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
            
    return (low + high) // 2


# Легаси-алиас: имя solve_dm_bps исторически считало simple margin (не discount).
# Оставлен для dev-скриптов; prod (services.valuation) зовёт solve_simple_margin_bps.
solve_dm_bps = solve_simple_margin_bps


def current_index_pct(coupons, calc_date: date, margin_bps: int, face_value: float,
                      amorts: Optional[List[dict]] = None,
                      base: Optional[str] = None,
                      hist: Optional[List[tuple]] = None) -> Optional[float]:
    """Текущий уровень базового индекса (КС/RUONIA), % — для discount margin
    (там индекс держится ПЛОСКИМ на текущем уровне, см. solve_discount_margin_bps).

    Источник #1 (при известной base) — ФАКТ ЦБ на calc_date. Это буквально то,
    что просит определение DM: сегодняшний уровень индекса.

    Источник #2 (фолбэк, если факта нет) — back-out из зафиксированного купона:
    ставка_купона = value·365/(days·face); индекс = ставка − маржа выпуска.
    Фолбэк систематически СТЕЙЛ и потому больше не первичен: 315 из 425 бумаг
    юниверса (74%) фиксируются в режиме `average` (дневной ресет, среднее по
    периоду) — back-out возвращает средний индекс ПРОШЕДШЕГО периода, а не
    сегодняшний уровень. Хуже: при average MOEX обычно не публикует value до
    конца периода, поэтому ветка `fixed_last` отдавала среднее ПОЗАПРОШЛОГО
    периода. На развороте ставки это ошибка в размер движения КС за 1–2 периода.

    face — номинал НА СТАРТЕ периода купона (для амортизируемых: остаток на
    calc_date + амортизации, выплаченные после start), иначе ставка из рублёвой
    суммы завышается."""
    if base in ("KEYRATE", "RUONIA"):
        try:
            rows = hist                 # инжектированная история [(date, pct),...]
            if rows is None:            # легаси-фолбэк: сам фетчит (CLI-пути)
                from services import cbr
                rows = cbr.ks_history() if base == "KEYRATE" else cbr.ruonia_history()
            val = None
            for md, r in rows:          # история отсортирована по дате
                if md <= calc_date:
                    val = r
                else:
                    break
            if val is not None:
                return round(float(val), 4)
        except Exception:
            pass

    def _d(s):
        if isinstance(s, date):
            return s
        try:
            return date.fromisoformat(s) if s else None
        except (ValueError, TypeError):
            return None

    def _face_at(start: date) -> float:
        add = 0.0
        for a in amorts or []:
            d = _amort_date(a)
            if d and a.get("value") is not None and start < d <= calc_date:
                add += float(a["value"])
        return face_value + add

    # Нормализуем и СОРТИРУЕМ по дате конца: порядок `coupons` не гарантирован
    # (в api.routes.bonds они собираются в порядке ответа MOEX), а ветка
    # fixed_last опирается на «последний зафиксированный» — без сортировки это
    # был произвольный элемент списка.
    norm = []
    for c in coupons or []:
        if isinstance(c, (tuple, list)):          # тройка (start, end, value)
            s, e = _d(c[0]), _d(c[1])
            v = c[2] if len(c) > 2 else None
        else:                                      # dict {start, end, value}
            s, e = _d(c.get("start")), _d(c.get("end"))
            v = c.get("value")
        if not s or not e or v is None:
            continue
        norm.append((s, e, v))
    norm.sort(key=lambda x: x[1])

    fixed_last = None
    for s, e, v in norm:
        face_p = _face_at(s)
        if face_p <= 0:
            continue
        rate = float(v) * 365.0 / (((e - s).days or 1) * face_p)
        if s <= calc_date < e:
            return round((rate - margin_bps / 10000.0) * 100.0, 4)
        if e <= calc_date:                 # только реально прошедшие периоды
            fixed_last = rate
    if fixed_last is not None:
        return round((fixed_last - margin_bps / 10000.0) * 100.0, 4)
    return None


def solve_discount_margin_bps(
    cashflows: List[Cashflow],
    calc_date: date,
    dirty_target_rub: float,
    index_flat_pct: float,
) -> Optional[int]:
    """Настоящий FRN discount margin (market-standard, met_float / Fabozzi).

    В отличие от нашего simple-margin-солвера (solve_simple_margin_bps, дисконт по форвард-кривой),
    здесь индекс держим ПЛОСКИМ на текущем уровне и дисконтируем money-market
    конвенцией: DF_i = Π_{k≤i} 1/(1 + (L + DM)·τ_k), τ ACT/365. Так pull-to-par
    дисконтируется правильно → DM выше simple на дисконте, ниже на премии (как НРД
    discount_margin). Купоны cashflows должны быть спроецированы на плоском L
    (build_cashflows на FlatForwardCurve(L)).

    Сверка с НРД discount_margin (scripts/dm_calibrate.py, off-par ликвид):
    med −20, m|Δ|≈47bps. Остаток — проприетарная fair-value машина НРД (NSS/Kalman),
    из публичных данных несводимо; направление и величина воспроизведены.
    """
    L = index_flat_pct / 100.0
    # якорь = дата поставки (T+1 раб), как у SM/XIRR
    settle = settle_date(calc_date)
    grid = sorted({cf.pay_date for cf in cashflows if cf.pay_date > settle})
    if not grid:
        return None
    amt_on = {}
    for cf in cashflows:
        if cf.pay_date > settle:
            amt_on[cf.pay_date] = amt_on.get(cf.pay_date, 0.0) + cf.amount_rub

    def pv(dm: float) -> float:
        r = L + dm
        df = 1.0
        prev = settle
        tot = 0.0
        for d in grid:
            tau = (d - prev).days / 365.0
            denom = 1.0 + r * tau
            if denom <= 0:
                return float("inf")
            df /= denom
            tot += amt_on[d] * df
            prev = d
        return tot

    lo, hi = -0.5, 2.5
    flo, fhi = pv(lo) - dirty_target_rub, pv(hi) - dirty_target_rub
    if flo != flo or fhi != fhi or flo * fhi > 0:  # NaN или нет корня
        return None
    for _ in range(90):
        mid = (lo + hi) / 2.0
        fm = pv(mid) - dirty_target_rub
        if abs(fm) < 1e-8:
            break
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2.0 * 10000)


class FlatForwardCurve(DiscountCurve):
    """Кривая с ПЛОСКИМ форвардом = const (текущий индекс). Для проекции купонов
    discount-margin: индекс не падает по форварду, держится на текущем уровне.
    Дисконт делает solve_discount_margin_bps сам — нужен только forward()."""
    rate_convention = "level"

    def __init__(self, calc_date: date, level_pct: float):
        self.calc_date = calc_date
        self._r = level_pct / 100.0

    def forward(self, t1: date, t2: date) -> float:
        return self._r

    def daily_forward(self, d: date) -> float:
        # плоский уровень: провайдер спеки (period_index_pct) зовёт
        # daily_forward по дням окна — без метода каждый будущий период
        # падал AttributeError и уходил на forward() с варнинг-спамом
        return self._r


def implied_yield_pct(
    bond: BondRefData, 
    curve: DiscountCurve, 
    cashflows: List[Cashflow], 
    calc_date: date, 
    dm_bps: int
) -> float:
    """
    Вычисляет эквивалентную доходность к погашению из DF_DM на дату maturity (implied yield).
    Возвращает значение в процентах годовых (e.g. 15.25 -> 15.25%).
    """
    _check_curve_convention(bond.base, curve)
    dm = dm_bps / 10000.0
    settle = settle_date(calc_date)     # якорь = дата поставки, как у солверов
    pay_dates_set = {cf.pay_date for cf in cashflows if cf.pay_date > settle}
    grid = sorted(list(pay_dates_set))

    df_dm = 1.0
    prev = settle
    df_dm_at_maturity = 1.0
    
    found_maturity = False
    
    for d in grid:
        days = (d - prev).days
        alpha = days / 365.0
        f_rate = curve.forward(prev, d)

        # та же конвенция, что pv_cashflows_with_dm
        if bond.base == "RUONIA":
            df_dm /= (1.0 + f_rate / 365.0)**days + dm * alpha
        elif bond.base == "KEYRATE":
            df_dm /= 1.0 + (f_rate + dm) * alpha

        if d == bond.maturity_date:
            df_dm_at_maturity = df_dm
            found_maturity = True
            break
            
        prev = d
        
    if not found_maturity or df_dm_at_maturity <= 0.0 or df_dm_at_maturity >= 1.0:
        return None   # maturity не в сетке платежей → None, не вводящий в заблуждение 0.0

    tau_days = (bond.maturity_date - settle).days
    if tau_days <= 0:
        return None

    tau_years = tau_days / 365.0
    
    # Эффективная годовая доходность (Effective Annual Yield), 
    # которая учитывает капитализацию / реинвестирование промежуточных выплат
    y_effective = math.pow(df_dm_at_maturity, -1.0 / tau_years) - 1.0
    return y_effective * 100.0


class _RuoniaCompoundPath:
    """Путь роллирования RUONIA от даты start: капитализация ТОЛЬКО в рабочие дни,
    внутри нерабочего окна — простое начисление по фиксингу последнего рабочего дня
    (деньги, отданные в репо в пятницу, три дня работают по простой ставке).

    Хранит по каждому фиксингу i: смещение дня days[i], накопленный рост cum[i]
    (start → день фиксинга) и ставку rates[i]. Рост до произвольной даты внутри
    окна — простым начислением от cum[i]. Путь строится лениво и кэшируется на
    объекте кривой: горизонт до 15 лет = ~3900 фиксингов, а зовём мы его на каждую
    бумагу универса (539) и на каждый уровень стакана.

    Ставку берём НЕ каждый день, а на границах узлов кривой: daily_forward
    кусочно-постоянен по сегментам (той же ступенью проецируются купоны), так что
    ~15 вызовов вместо тысяч.

    ДЕЛИТЕЛЬ — ACT/ACT: длина года берётся ФАКТИЧЕСКАЯ (366 в високосный), как в
    официальном индексе ЦБ. На фиксированных 365 расчёт уезжал вверх ровно в
    високосные годы (сверено с RuoniaSV на истории с 2010: расхождение копилось
    только в 2012/2016/2020/2024, после ACT/ACT упало с 10.9 до 0.5 bps за 16
    лет). Смещение зависело от доли високосных дней в горизонте — 0 bps до года
    и до −1.4 bps на 3 годах, то есть искажало сравнение бумаг РАЗНЫХ сроков
    между собой. Аннуализация остаётся 365-й (^(365/D)) — как у XIRR бумаги,
    иначе сравнение двух ног станет асимметричным."""
    __slots__ = ("curve", "start", "days", "cum", "rates", "_bounds", "_r", "_hi")

    def _year_frac(self, d0: int, n: int) -> float:
        """Σ 1/длина_года(день) по n дням, начиная со смещения d0 от start.
        Окна короткие (1–4 дня, длинные праздники до ~10), цикл дешёвый."""
        acc, d = 0.0, self.start + timedelta(days=d0)
        for _ in range(n):
            acc += 1.0 / (366.0 if _cal.isleap(d.year) else 365.0)
            d += timedelta(days=1)
        return acc

    def __init__(self, curve: DiscountCurve, start: date):
        self.curve = curve
        self.start = start
        self._bounds = [nd for nd, _ in getattr(curve, "nodes", []) or []]
        self._r = self._hi = None
        self.days = [0]
        self.cum = [1.0]
        self.rates = [self._rate_at(start)]

    def _rate_at(self, d: date) -> float:
        if self._hi is not None and d < self._hi:
            return self._r
        self._r = self.curve.daily_forward(d)
        self._hi = next((nd for nd in self._bounds if nd > d), None)
        return self._r

    def _extend_to(self, n: int) -> None:
        while self.days[-1] < n:
            d0 = self.days[-1]
            nxt = self.start + timedelta(days=d0 + 1)
            while _is_settlement_day_off(nxt):
                nxt += timedelta(days=1)
            k = (nxt - self.start).days
            self.cum.append(self.cum[-1] * (1.0 + self.rates[-1] * self._year_frac(d0, k - d0)))
            self.days.append(k)
            self.rates.append(self._rate_at(nxt))

    def growth_to(self, target: date) -> float:
        n = (target - self.start).days
        if n <= 0:
            return 1.0
        self._extend_to(n)
        i = bisect_right(self.days, n) - 1
        return self.cum[i] * (1.0 + self.rates[i] * self._year_frac(self.days[i], n - self.days[i]))


def _ruonia_path(curve: DiscountCurve, start: date) -> _RuoniaCompoundPath:
    """Кэш пути на объекте кривой: кривые живут день (или пере-собираются на
    прошлую дату), путь для одного start общий на весь универс."""
    cache = getattr(curve, "_ru_paths", None)
    if cache is None:
        cache = {}
        try:
            curve._ru_paths = cache
        except AttributeError:          # __slots__-кривая — считаем без кэша
            return _RuoniaCompoundPath(curve, start)
    p = cache.get(start)
    if p is None:
        p = cache[start] = _RuoniaCompoundPath(curve, start)
    return p


def ruonia_rolling_yield_pct(
    curve: DiscountCurve,
    calc_date: date,
    maturity: date,
) -> Optional[float]:
    """Эффективная годовая доходность РОЛЛИРОВАНИЯ RUONIA по ожидаемым будущим
    ставкам (OIS-кривая), calc_date → maturity.

    ЕДИНАЯ база сравнения (base leg) Y-IDX для ВСЕХ флоатеров — и КС, и RUONIA
    (решение 2026-08-04): альтернатива держателю бумаги одна — раздавать деньги
    o/n, а это RUONIA, независимо от того, к какому индексу привязан купон. Раньше
    КС-бумаги сравнивались с квартальным роллированием КС, RUONIA-бумаги — с
    дневным компаундингом RUONIA: два разных базиса, и Y-IDX двух бумаг был
    несопоставим (разница конвенций сидела в цифре как ложный спред).

    Конвенция роста — фактическая механика o/n-размещения: капитализация в
    рабочие дни, нерабочее окно — простое начисление по фиксингу последнего
    рабочего дня (пятничный фиксинг работает 3 дня простыми, без капитализации
    внутри). Рабочие дни — календарь расчётов MOEX (_is_settlement_day_off);
    расхождения с календарём ЦБ единичны и на эффективную годовую не влияют.

    Итог приводим к ЭФФЕКТИВНОЙ ГОДОВОЙ: рост^(365/D)−1 — та же аннуализация,
    что у XIRR бумаги ((1+y)^t), поэтому разность доходностей (bps) корректна.
    Возвращает % годовых или None."""
    # горизонт от даты поставки (T+1 раб) — тот же якорь, что у XIRR бумаги,
    # иначе разность «бумага − индекс» несёт паразитный сдвиг на выходных
    start = settle_date(calc_date)
    D = (maturity - start).days
    if D <= 0:
        return None
    growth = _ruonia_path(curve, start).growth_to(maturity)
    if growth <= 0.0:
        return None
    return (math.pow(growth, 365.0 / D) - 1.0) * 100.0


# -------------------------------------------------------------------------
# 3) Выходной API Pipeline
# -------------------------------------------------------------------------

def calculate_floater_metrics(
    bond: BondRefData, 
    price: float, # чистая цена (pct) %
    curve: DiscountCurve, 
    calc_date: date
) -> dict:
    """
    Полный пайплайн для расчёта аналитики для одной бумаги (для Last/Bid/Ask или уровня стакана).
    Легаси-путь (CLI last_prices); прод — services.valuation.calculate_valuation_metrics.
    """
    # Перп (нет maturity) — генератор дат упадёт; погашение ≤ T+1 — весь поток ex,
    # XIRR против одного редемпшна = мусор. Те же guard'ы, что в прод-пути.
    if bond.maturity_date is None or bond.maturity_date <= settle_date(calc_date):
        return {
            "clean_price_pct": price, "accrued_rub": bond.accrued_rub, "dirty_rub": None,
            "dm_bps": None, "spread_to_base_bps": None, "implied_yield_pct": None,
            "base_yield_pct": None, "spread_issue_bps": bond.spread_issue_bps,
            "yield_base_type": bond.base,
        }

    dirty_rub = dirty_price_rub(bond.face_value, price, bond.accrued_rub)

    cfs = build_cashflows_with_spread(bond, curve, calc_date, bond.spread_issue_bps)
    base_cfs = build_cashflows_with_spread(bond, curve, calc_date, 0)

    impl_yield = xirr_yield_pct(dirty_rub, cfs, calc_date)
    base_yield = xirr_yield_pct(dirty_rub, base_cfs, calc_date)

    spread_to_base_bps = None
    if impl_yield is not None and base_yield is not None:
        spread_to_base_bps = round((impl_yield - base_yield) * 100.0)
        
    return {
        "clean_price_pct": price,
        "accrued_rub": bond.accrued_rub,
        "dirty_rub": dirty_rub,
        "dm_bps": spread_to_base_bps,
        "spread_to_base_bps": spread_to_base_bps,
        "implied_yield_pct": impl_yield,
        "base_yield_pct": base_yield,
        "spread_issue_bps": bond.spread_issue_bps,
        "yield_base_type": bond.base 
    }


# ========================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ (Smoke tests / Local checks)
# ========================================================================
if __name__ == "__main__":
    from datetime import timedelta

    calc_d = date(2024, 7, 1)
    
    # Мок-кривая 10% Flat Forward
    class FlatCurveMock(DiscountCurve):
        rate_convention = "level"          # сырой уровень, не simple-форвард

        def forward(self, t1: date, t2: date) -> float:
            return 0.10
            
    mock_curve = FlatCurveMock(calc_date=calc_d, nodes=[(calc_d, 1.0)])
    
    # 1. Тест дат генерации:
    # 1-й купон - 2024-09-01, Quarterly. maturity = 2025-06-01
    c_dates = generate_coupon_dates(
        first_coupon_date=date(2024, 9, 1),
        maturity_date=date(2025, 6, 1), 
        coupons_per_year=4
    )
    print(f"Coupon Dates: {c_dates}")
    assert c_dates == [date(2024, 9, 1), date(2024, 12, 1), date(2025, 3, 1), date(2025, 6, 1)]

    # 2. Мок RUONIA бумаги (spread = 150 bps)
    mock_bond = BondRefData(
        isin="RU000A10X0M4",
        base="RUONIA",
        spread_issue_bps=150,
        face_value=1000.0,
        accrued_rub=8.5,
        maturity_date=date(2030, 6, 1),
        first_coupon_date=date(2024, 9, 1),
        coupons_per_year=4,
        issue_date=date(2024, 6, 1)
    )

    # Cashflows Builder Test
    cfs = build_cashflows_to_maturity(mock_bond, mock_curve, calc_d)
    print(f"\nCashflows built: {len(cfs)}")
    for cf in cfs:
        print(f" - {cf.pay_date} | {cf.type} | {cf.amount_rub:,.2f} RUB")
        
    # Dirty Price
    # Если мы торгуемся по номиналу (clean=100), значит DM (спред к кривой) должен быть равен spread_issue (150 bps).
    dirty_p = dirty_price_rub(1000.0, 100.0, mock_bond.accrued_rub)
    
    # Solver test
    dm_calculated = solve_simple_margin_bps(mock_bond, mock_curve, cfs, calc_d, dirty_p)
    print(f"\nSolved DM for Par Price: {dm_calculated} bps (Expected ~150 bps)")
    
    if dm_calculated is not None:
        y_pct = implied_yield_pct(mock_bond, mock_curve, cfs, calc_d, dm_calculated)
        print(f"Implied Yield out: {y_pct if y_pct is None else round(y_pct, 4)} %")
