from datetime import date
from typing import Dict, Any, Optional

from core.valuation import BondRefData
from core.cashflow import parse_base_and_spread
from core.forwards import add_months


_BASE_LABEL = {"KEYRATE": "Ключевая ставка", "RUONIA": "RUONIA"}


def _to_date(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def apply_registry_params(ref: BondRefData) -> BondRefData:
    """Реестр инструментов — ЕДИНСТВЕННЫЙ источник истины расчётных параметров.
    isins_cache/MOEX-справочник лишь заполняют пробелы: непустое значение реестра
    побеждает. Иначе правка Справочника не доезжала до расчёта — маржа/период/
    даты продолжали жить в стейлом MOEX-дампе (FORMULA/COUPONPERIOD), а реестр
    спрашивали только при base=UNKNOWN."""
    try:
        from services import instruments_registry as _reg
        row = _reg.calc_params_map().get(ref.isin)
    except Exception:
        row = None
    if not row:
        return ref
    if row.get("base") in ("RUONIA", "KEYRATE"):
        ref.base = row["base"]
    if row.get("margin_bps") is not None:
        ref.spread_issue_bps = int(row["margin_bps"])
    d = _to_date(row.get("maturity_date"))
    if d:
        ref.maturity_date = d
    d = _to_date(row.get("issue_date"))
    if d:
        ref.issue_date = d
    if row.get("coupon_period_days"):
        ref.coupon_period_days = int(row["coupon_period_days"])
    if row.get("coupons_per_year"):
        ref.coupons_per_year = int(row["coupons_per_year"])
    if row.get("face_value"):
        ref.face_value = float(row["face_value"])
    # first_coupon_date производён от issue+шага — пересчёт от финальных значений
    if ref.issue_date and ref.coupons_per_year and 1 <= ref.coupons_per_year <= 12:
        ref.first_coupon_date = add_months(ref.issue_date, 12 // ref.coupons_per_year)
    return ref


def build_ref_external(isin: str, mo: dict, base: Optional[str] = None,
                       spread_bps: Optional[int] = None) -> BondRefData:
    """Строит BondRefData для ПРОИЗВОЛЬНОЙ бумаги (нет в isins_cache).
    Справочник — MOEX ISS; база+спред флоатера — из Cbonds-справки (ref_data),
    либо явно переданные base/spread_bps (напр. из universe-строки реестра)."""
    mo = mo or {}
    try:
        face = float(mo.get("face") or 1000)
    except (ValueError, TypeError):
        face = 1000.0
    try:
        cp = int(mo.get("coupon_period")) if mo.get("coupon_period") else None
    except (ValueError, TypeError):
        cp = None
    try:
        accrued = float(mo.get("accrued")) if mo.get("accrued") is not None else 0.0
    except (ValueError, TypeError):
        accrued = 0.0

    base = base or "UNKNOWN"
    spread = int(spread_bps) if spread_bps is not None else 0

    # Cbonds-справка (ref_data) как источник базы/маржи: точная маржа сверена
    # 317/326 ±5бп; заполняет то, чего нет в переданных base/spread.
    try:
        from services.ref_data import params as _ref_params
        rp = _ref_params(isin)
        if base in (None, "UNKNOWN") and rp.get("base"):
            base = rp["base"]
        if spread == 0 and rp.get("margin_bps") is not None:
            spread = int(rp["margin_bps"])
    except Exception:
        pass

    issue = _to_date(mo.get("issue"))
    maturity = _to_date(mo.get("maturity"))
    cpy = round(365 / cp) if cp else 4
    first_coupon = add_months(issue, 12 // cpy) if (issue and cpy) else None

    # Реестр — источник истины: непустые поля реестра побеждают MOEX/Cbonds.
    # Закрывает и старые дыры: base=UNKNOWN у бумаг вне isins_cache (ЕАБР П3-07)
    # и пустой mo у бумаг с отдельным SECID (ОФЗ-ПК RU000A0JV4P3 → SU29008),
    # где без maturity рвался valuation.
    return apply_registry_params(BondRefData(
        isin=isin, base=base, spread_issue_bps=spread, face_value=face,
        accrued_rub=accrued, maturity_date=maturity, first_coupon_date=first_coupon,
        coupons_per_year=cpy, issue_date=issue, coupon_period_days=cp,
    ))


def implied_current_face(coupons_full, calc_date: date) -> Optional[float]:
    """Истинный текущий номинал из ЗАФИКСИРОВАННОГО купона:
        face = value / (valueprc/100 · days/365).
    value (рубли) и valueprc (ставка %) — твёрдый факт MOEX bondization, из них
    номинал восстанавливается точно (в отличие от поля face строк купонов, которое
    бывает стейл 1000, и от MOEX FACEVALUE, которого может не быть в кэше).
    Берём купон, накрывающий calc_date (для амортизируемых — остаток на текущий
    период); иначе последний прошедший. None — восстановить нельзя."""
    best = None  # (end_date, implied)
    for c in coupons_full or []:
        v = c.get("value")
        vp = c.get("valueprc")
        s = _to_date(c.get("start"))
        e = _to_date(c.get("end"))
        if v is None or not vp or not s or not e:
            continue
        try:
            days = (e - s).days or 1
            imp = float(v) / (float(vp) / 100.0 * days / 365.0)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
        if s <= calc_date < e:
            return imp                      # текущий период — точный остаток
        if e <= calc_date and (best is None or e > best[0]):
            best = (e, imp)
    return best[1] if best else None


def reconcile_face(ref: BondRefData, coupons_full, calc_date: date,
                   ratio: float = 3.0) -> Optional[float]:
    """Правит ref.face_value ТОЛЬКО при мисскейле В РАЗЫ (order-of-magnitude):
    тихий фолбэк на дефолтные 1000, когда бумаги нет в securities-кэше, а реальный
    номинал 1 млн / 10 млн. Возвращает старый номинал, если правка была, иначе None.

    Почему только кратный разрыв, а не любой: implied_current_face восстанавливает
    номинал НА СТАРТ купонного периода (из value/valueprc). У амортизируемых бумаг
    он ВЫШЕ текущего остатка, от которого котируется цена (амортизация в середине
    периода) — правка на него испортила бы верный securities-остаток (наблюдалось
    RU000A109XR1: 900 остаток → 985 старт-периода). Промахи деноминации всегда
    кратны 10^k (1000 vs 10 млн = 10000×), поэтому порог в 3× чисто отделяет их от
    амортизационных нюансов (<2×) и шума округления valueprc (<1.01×)."""
    imp = implied_current_face(coupons_full, calc_date)
    if imp is None or imp <= 0 or not ref.face_value or ref.face_value <= 0:
        return None
    r = max(imp / ref.face_value, ref.face_value / imp)
    if r > ratio:
        old = ref.face_value
        ref.face_value = round(imp, 2)
        return old
    return None


def yidx_at_price(row: dict, price: Optional[float]) -> Optional[float]:
    """Y-IDX по ПРОИЗВОЛЬНОЙ цене выпуска — наклоном от уже посчитанного спреда
    цены сделки (yoi при last). Тот же приём, что даёт спред верха стакана
    (yoi_bid/yoi_ask) и уровней в стакане: на масштабе одного дня Y-IDX(цена)
    практически прямая, а полный reprice стоил бы сборки модели на бумагу.

    Нужен спреду по СРЕДНЕВЗВЕСУ дня: last price — это одна, возможно случайная
    сделка (в неликвиде — тонкий принт на закрытии), а средневзвес взвешен
    объёмом и куда устойчивее как «цена дня»."""
    slope, yoi, last = row.get("yoi_slope"), row.get("yoi"), row.get("last")
    if price is None or slope is None or yoi is None or last is None or price <= 0:
        return None
    return int(round(yoi + (price - last) * slope))


def amort_remaining_face(amorts, calc_date: date,
                        current_face: Optional[float] = None) -> Optional[float]:
    """Остаток номинала на calc_date из графика амортизаций MOEX = Σ будущих
    траншей (вкл. финальное погашение). Авторитетнее кэша securities: isins_cache
    у амортизируемых бумаг стейлится (БалтЛизП10: кэш 1000₽ при остатке 900₽ →
    dirty/SM/DM карточки и бэктест паспорта завышали номинал на 11%).
    None — графика нет (не амортизируется или нет данных), берите прежний face.

    НЕПОЛНЫЙ ГРАФИК ТОЖЕ ДАЁТ None. У ипотечных агентов MOEX публикует только
    объявленные транши, дальние стоят нулями: сумма всего графика оказывается
    много меньше номинала, и «Σ будущих» превращается в копейки (sИАДОМ1P19
    24.08: 8,78₽ против биржевых 577,64₽ — цена в % от такого «номинала»
    давала Y-IDX в тысячи bps). Сверяем сумму графика с текущим номиналом:
    не сходится — доверяем бирже, а не арифметике по огрызку."""
    if not amorts:
        return None
    tot = whole = 0.0
    for a in amorts:
        d = a.get("date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except (ValueError, TypeError):
                continue
        if not isinstance(d, date) or a.get("value") is None:
            continue
        v = float(a["value"])
        whole += v
        if d > calc_date:
            tot += v
    if current_face and whole < current_face * 0.95:
        return None
    return tot if tot > 0 else None


def external_formula(ref: BondRefData) -> str:
    """Синтетическая формула купона для отображения (нет FORMULA из кэша)."""
    label = _BASE_LABEL.get(ref.base, ref.base)
    if ref.spread_issue_bps:
        return f"{label} + {ref.spread_issue_bps / 100:g}%"
    return label


def coupons_per_year(period_days: Optional[int], declared: Optional[int] = None) -> Optional[int]:
    """Купонов в год для витрины. Приоритет — фактический период купона
    (coupon_period_days считается из реального графика выплат и переживает
    кривой COUPONFREQUENCY в справочниках), декларированная частота — фолбэк."""
    try:
        if period_days and int(period_days) > 0:
            return max(1, min(365, round(365 / int(period_days))))
    except (TypeError, ValueError):
        pass
    try:
        return int(declared) or None
    except (TypeError, ValueError):
        return None


def next_coupon_after(ref_obj: BondRefData, today: date) -> Optional[date]:
    """Ближайшая купонная дата >= today (шагаем от первого купона по периоду).
    Для списка — корректная замена first_coupon_date (тот часто в прошлом)."""
    d = ref_obj.first_coupon_date
    if not d:
        return ref_obj.maturity_date
    # см. core/valuation.generate_coupon_dates: при cpy>12 шаг 0 не двигает
    # дату, и цикл ниже крутится до guard'а, возвращая дату погашения
    # вместо ближайшего купона (ВЭБP-46 с 14-дневным купоном)
    step = max(1, 12 // (ref_obj.coupons_per_year or 4))
    guard = 0
    while d < today and guard < 600:
        if ref_obj.maturity_date and d >= ref_obj.maturity_date:
            return ref_obj.maturity_date
        d = add_months(d, step)
        guard += 1
    if ref_obj.maturity_date and d > ref_obj.maturity_date:
        return ref_obj.maturity_date
    return d

def create_bond_ref_data(data: dict, isin: str) -> BondRefData:
    """Helper to convert cache dict to BondRefData."""
    try:
        fv = float(data.get("FACEVALUE", 1000))
    except (ValueError, TypeError):
        fv = 1000.0

    try:
        start_date = date.fromisoformat(data.get("STARTDATE", ""))
    except (ValueError, TypeError):
        start_date = None

    try:
        end_date = date.fromisoformat(data.get("ENDDATE", "") or data.get("MATDATE", ""))
    except (ValueError, TypeError):
        end_date = None

    try:
        coupon_period_days = int(data.get("COUPONPERIOD", ""))
    except (ValueError, TypeError):
        coupon_period_days = None

    try:
        if data.get("FREQUENCY"):
            frequency = int(data.get("FREQUENCY"))
        else:
            frequency = None
    except (ValueError, TypeError):
        frequency = None

    try:
        next_coupon_date = date.fromisoformat(data.get("NEXTCOUPON")) if data.get("NEXTCOUPON") else None
    except (ValueError, TypeError):
        next_coupon_date = None
        
    acc_str = data.get('ACCRUEDINT')
    accrued = float(acc_str) if acc_str is not None else 0.0

    formula = data.get("FORMULA", "")
    base_rate_str = data.get("BASE_RATE")
    base, spread_bps = parse_base_and_spread(formula, base_rate_str)

    # База/маржа из реестра приходят ниже через apply_registry_params (батч).

    # Exact fallback from CLI for first coupon mapping
    step_months = max(1, 12 // (frequency or 4))   # MOEX FREQUENCY>12 → шаг 0
    from core.forwards import add_months
    first_coupon = None
    if start_date:
        first_coupon = add_months(start_date, step_months)

    # Реестр — источник истины: правки Справочника (маржа/период/даты/номинал)
    # обязаны побеждать значения из isins_cache (MOEX-дамп, парс FORMULA).
    return apply_registry_params(BondRefData(
        isin=isin,
        base=base or "UNKNOWN",
        spread_issue_bps=spread_bps,
        face_value=fv,
        accrued_rub=accrued,
        maturity_date=end_date,
        first_coupon_date=first_coupon,
        coupons_per_year=frequency or 4,
        issue_date=start_date,
        coupon_period_days=coupon_period_days
    ))

def extract_bond_reference_dict(isin: str, data: dict, ref_obj: BondRefData) -> Dict[str, Any]:
    next_coupon_date = None
    try:
        if data.get("NEXTCOUPON"):
            next_coupon_date = date.fromisoformat(data.get("NEXTCOUPON"))
    except (ValueError, TypeError):
        pass
        
    return {
        "isin": isin,
        "short_name": data.get("SHORTNAME", ""),
        "face_value": ref_obj.face_value,
        "face_unit": data.get("FACEUNIT", "RUB"),
        "base_rate_type": ref_obj.base,
        "spread_bps": ref_obj.spread_issue_bps,
        "formula": data.get("FORMULA", ""),
        "start_date": ref_obj.issue_date,
        "maturity_date": ref_obj.maturity_date,
        "coupon_period_days": ref_obj.coupon_period_days,
        "coupons_per_year": ref_obj.coupons_per_year,
        "next_coupon_date": next_coupon_date,
        "accrued_interest": ref_obj.accrued_rub
    }
