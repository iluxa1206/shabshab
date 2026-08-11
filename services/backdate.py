"""Метрики флоатера НА ПРОШЛУЮ ДАТУ: (isin, дата D, цена) → SM/DM/y-idx/YTM.

Честность входов на D:
  • цена     — задаётся пользователем ИЛИ close/legalclose из MOEX history;
  • НКД      — факт ACCINT из MOEX history на D (фолбэк: НКД из графика купонов);
  • номинал  — факт FACEVALUE из MOEX history на D (амортизация учтена биржей);
  • кривая   — curve_asof(): архив своп-котировок на дату ≤ D (mode="market",
    копится curve_history) → честный bootstrap; иначе ГИБРИД (mode="realized"):
    сегмент D→сегодня из реализованного дневного факта индекса ЦБ (КС/RUONIA),
    дальше — сегодняшняя bootstrap-кривая, сшитая по DF. Для прошедшего отрезка
    реализованный факт точнее любой тогдашней рыночной кривой (эти купоны уже
    зафиксированы именно так); хвост — сегодняшние ожидания.
  • фиксинги — история индекса ЦБ покрывает D→сегодня фактом, начавшиеся на D
    периоды проецируются по факту (та же реализованная философия, что кривая).

Расчёт — тот же calculate_valuation_metrics, что прайсит live-таблицу:
результаты сопоставимы с текущими SM/DM/y-idx колонка-в-колонку.
"""
from __future__ import annotations

import asyncio
import logging
import math
from bisect import bisect_right
from datetime import date, timedelta
from typing import Optional, Tuple

import httpx

from core.forwards import BootstrappedForwardCurve, CurveBootstrapper, DiscountCurve
from services.exceptions import NotFoundException, CalculationException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- кривая as-of

class SplicedAsofCurve(DiscountCurve):
    """Гибридная кривая на прошлую дату D: [eff=D+1 … splice) — дневной ФАКТ
    индекса (ступень: последняя ставка ЦБ ≤ t), от splice — ДЕЛЕГИРОВАНИЕ
    anchor-кривой (её forward/df/конвенция без изменений).

    Раньше гибрид пересобирал узлы anchor в BootstrappedForwardCurve — DF
    совпадали, но forward() на годовых+ сегментах считался другой конвенцией:
    sheet-кривая идёт по пути уровней (KEYRATE >1Y — квартальный номинал из
    годового par), а Bootstrapped давал простую ставку фактора, «раздутую»
    компаундингом (~+70bp на сегменте 1Y→2Y при КС ~13%). Дальние купоны
    realized-дат завышались, и серия спреда была смещена на десятки bps
    относительно market-дат (ТрансмхПБ8 28→29.07: −50bp «скачок»)."""

    def __init__(self, base: str, eff: date, splice: date, anchor: DiscountCurve,
                 dates: list, rates: list):
        self.base_type = base
        self.rate_convention = anchor.rate_convention
        self._anchor = anchor
        self._splice = splice
        self._dates, self._rates = dates, rates
        # факт-фактор по дням до splice + кумулятив для df()/окон
        self._cum = {}          # date → Π(1+r/365) от eff до date (не включая)
        f = 1.0
        d = eff
        while d <= splice:
            self._cum[d] = f
            if d == splice:
                break
            f *= 1.0 + (self._rate_at(d) / 100.0) / 365.0
            d += timedelta(days=1)
        self._fact_total = self._cum[splice]
        super().__init__(eff, [(splice, 1.0 / self._fact_total)])

    def _rate_at(self, d: date) -> float:
        i = bisect_right(self._dates, d) - 1
        return self._rates[i]

    def _fact_factor(self, t1: date, t2: date) -> float:
        """Π(1+r/365) факт-сегмента на [t1, t2) внутри [eff, splice]."""
        return self._cum[t2] / self._cum[t1]

    def df(self, d: date) -> float:
        if d <= self.calc_date:
            return 1.0
        if d <= self._splice:
            if d in self._cum:
                return 1.0 / self._cum[d]
            # дата внутри факт-сегмента, но вне сетки (не бывает: сетка дневная)
            return super().df(d)
        return (1.0 / self._fact_total) * self._anchor.df(d)

    def daily_forward(self, d: date) -> float:
        if d >= self._splice:
            return self._anchor.daily_forward(d)
        # факт ЦБ хранится в процентах, контракт daily_forward — доли
        return self._rate_at(max(d, self.calc_date)) / 100.0

    def forward(self, t1: date, t2: date) -> float:
        if t1 >= t2:
            raise ValueError("t2 must be strictly > t1")
        if t1 >= self._splice:
            return self._anchor.forward(t1, t2)          # конвенция anchor как есть
        a, b = max(t1, self.calc_date), min(t2, self._splice)
        n_fact = (b - a).days
        n_anchor = (t2 - self._splice).days if t2 > self._splice else 0
        n = (t2 - t1).days
        if self.base_type == "KEYRATE":
            # simple ACT/365 — средневзвешенный по дням уровень окна (как sheet)
            lvl_sum = 0.0
            d = a
            while d < b:
                lvl_sum += self._rate_at(d) / 100.0
                d += timedelta(days=1)
            if n_anchor > 0:
                lvl_sum += self._anchor.forward(self._splice, t2) * n_anchor
            return lvl_sum / n
        # RUONIA daily-comp: факторы факт-части и anchor-части перемножаются
        ln = math.log(self._fact_factor(a, b))
        if n_anchor > 0:
            ln += math.log(self._anchor.df(self._splice) / self._anchor.df(t2))
        return 365.0 * (math.exp(ln / n) - 1.0)


def build_hybrid_curve(base: str, calc_date: date, hist_pairs: list,
                       today_curve: DiscountCurve) -> DiscountCurve:
    """Гибридная кривая на прошлую дату D: факт индекса от D+1 до старта
    anchor-кривой, дальше — сама anchor-кривая (см. SplicedAsofCurve)."""
    eff = calc_date + timedelta(days=1)            # T+1, как effective start bootstrap
    splice = today_curve.calc_date                  # effective start anchor-кривой
    if eff >= splice:
        return today_curve                          # D сегодня/вчера — гибрид не нужен

    dates = [d for d, _ in hist_pairs]
    rates = [r for _, r in hist_pairs]
    if not dates or dates[0] > eff:
        raise CalculationException(
            f"история {base} не покрывает {eff.isoformat()} — гибридная кривая невозможна")

    return SplicedAsofCurve(base, eff, splice, today_curve, dates, rates)


_anchor_memo: dict = {}    # (base, first_archive_date) → кривая; архив прошлого не меняется


def curve_asof(base: str, calc_date: date, today_curve: DiscountCurve,
               hist_pairs: list) -> Tuple[DiscountCurve, str]:
    """Кривая на прошлую дату + режим ('market' архив котировок / 'realized' гибрид)."""
    try:
        from services.curve_history import quotes_asof
        quotes = quotes_asof(base, calc_date)
    except Exception as e:
        logger.warning(f"curve_history quotes_asof failed: {e}")
        quotes = None
    if quotes:
        try:
            from core.forwards import SheetForwardCurve
            # sheet-методика — консистентно с живым прайсингом (иначе скачок в динамике)
            return SheetForwardCurve(calc_date, quotes, base), "market"
        except Exception as e:
            logger.warning(f"as-of bootstrap {base}@{calc_date} failed: {e} — фолбэк на гибрид")
    # Даты ДО начала архива: якорь гибрида — ПЕРВАЯ архивная кривая, не
    # сегодняшняя. Свопы дрейфуют, и хвост сегодняшней кривой рвал серию скачком
    # ровно на границе архива (ТрансмхПБ8 28→29.07: −50bp Y-IDX при флэт цене);
    # с якорем в первой архивной дате гибрид на границе сходится к ней по
    # построению. Хвост за якорем — рынок той даты, ближайший к D из доступных.
    anchor = today_curve
    try:
        from services.curve_history import quotes_first
        first = quotes_first(base)
        if first and first[0] > calc_date:
            key = (base, first[0])
            a = _anchor_memo.get(key)
            if a is None:
                from core.forwards import SheetForwardCurve
                a = _anchor_memo[key] = SheetForwardCurve(first[0], first[1], base)
            anchor = a
    except Exception as e:
        logger.warning(f"anchor curve {base}: {e} — гибрид на сегодняшней кривой")
    return build_hybrid_curve(base, calc_date, hist_pairs, anchor), "realized"


# ------------------------------------------------------- дневная строка MOEX

async def fetch_history_row(secid: str, d: date, board: str = "TQCB") -> Optional[dict]:
    """Строка MOEX history на дату ≤ d (ближайший торговый день, до 10 дн назад):
    {date, close, legalclose, accint, facevalue}. None — торгов не было/бумаги нет."""
    url = (f"https://iss.moex.com/iss/history/engines/stock/markets/bonds/"
           f"boards/{board}/securities/{secid}.json")
    params = {
        "iss.meta": "off",
        "from": (d - timedelta(days=10)).isoformat(),
        "till": d.isoformat(),
        "history.columns": "TRADEDATE,CLOSE,LEGALCLOSEPRICE,ACCINT,FACEVALUE",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=8)
        if resp.status_code != 200:
            return None
        h = resp.json().get("history", {})
        cols, data = h.get("columns", []), h.get("data", [])
        if not data:
            return None
        idx = {c: i for i, c in enumerate(cols)}
        row = data[-1]                              # последняя строка ≤ d
        g = lambda c: row[idx[c]] if c in idx else None
        return {
            "date": g("TRADEDATE"),
            "close": g("CLOSE"),
            "legalclose": g("LEGALCLOSEPRICE"),
            "accint": g("ACCINT"),
            "facevalue": g("FACEVALUE"),
        }
    except Exception as e:
        logger.warning(f"MOEX history fetch {secid}@{d}: {e}")
        return None


def _period_at(periods, d: date) -> Optional[tuple]:
    """Купонный период (start, end, value|None), накрывающий d: start ≤ d < end."""
    for c in periods or []:
        try:
            s = c[0] if isinstance(c, (tuple, list)) else c.get("start")
            e = c[1] if isinstance(c, (tuple, list)) else c.get("end")
            v = c[2] if isinstance(c, (tuple, list)) else c.get("value")
            s = date.fromisoformat(s) if isinstance(s, str) else s
            e = date.fromisoformat(e) if isinstance(e, str) else e
        except Exception:
            continue
        if s and e and e > s and s <= d < e:
            return (s, e, v)
    return None


def _accrued_from_periods(periods, d: date, face: float) -> Optional[float]:
    """Фолбэк-НКД из графика купонов: value·(d−start)/(end−start) периода, где start≤d<end."""
    p = _period_at(periods, d)
    if p and p[2] is not None:
        s, e, v = p
        return float(v) * (d - s).days / (e - s).days
    return None


def _accrued_estimate(periods, d: date, face: float, index_pct: Optional[float],
                      margin_bps: int) -> Optional[float]:
    """Последний рубеж, когда НКД неоткуда взять: купон периода ещё НЕ опубликован
    (value=None) и биржевого ACCINT на дату нет. Начисляем по конвенции выпуска
    (индекс + маржа) simple ACT/365 от факта индекса ЦБ на d."""
    p = _period_at(periods, d)
    if not p or index_pct is None:
        return None
    s, _e, _v = p
    rate = (index_pct / 100.0) + (margin_bps or 0) / 10000.0
    return face * rate * (d - s).days / 365.0


def _accrue_to_date(accint_fact: float, trade_date: date, d: date, periods,
                    face: float) -> Tuple[float, Optional[str]]:
    """НКД на календарную дату d из биржевого факта на trade_date ≤ d.

    MOEX history отдаёт последнюю ТОРГОВУЮ строку ≤ d, а calc_date остаётся d:
    на выходных/праздниках НКД замирал на пятнице, хотя купон капает каждый день
    (наблюдалось: 2 дня × 0.41₽ на номинале 900 ≈ 9bps цены → SM/YTM смещены).
    Доначисляем от факта:
      • тот же купонный период и купон опубликован → факт + value·Δдней/период;
      • период тот же, купон не опубликован → пропорция по дням от самого факта
        (не требует ни прогноза, ни спеки);
      • период сменился (в промежутке выплата) → чистое начисление нового
        периода из графика.
    """
    gap = (d - trade_date).days
    if gap <= 0:
        return accint_fact, None
    p_t, p_d = _period_at(periods, trade_date), _period_at(periods, d)
    note = (f"НКД доначислен с {trade_date.isoformat()} (последние торги) до "
            f"{d.isoformat()}: {gap} нерабочих дн")
    if p_t and p_d and p_t[0] == p_d[0]:
        s, e, v = p_d
        if v is not None:
            daily = float(v) / ((e - s).days or 1)
            return round(accint_fact + daily * gap, 4), note
        elapsed = (trade_date - s).days
        if elapsed > 0:
            return round(accint_fact * (d - s).days / elapsed, 4), note
    sched = _accrued_from_periods(periods, d, face)
    if sched is not None:
        return round(sched, 4), note + " (по графику купонов: в промежутке выплата)"
    return accint_fact, ("НКД не удалось доначислить до даты расчёта — взят факт "
                         f"на {trade_date.isoformat()}, занижен на {gap} дн")


# ------------------------------------------------------------------ контекст

async def resolve_market(isin: str, board: Optional[str] = None) -> Tuple[str, str]:
    """(SECID, борд) для history-эндпоинта. У корпоратов SECID == ISIN и борд
    TQCB/TQRD, у ОФЗ — SU29…/TQOB (по ISIN history отдаёт 0 строк → раньше весь
    as-of путь для ОФЗ-ПК падал на «НКД не восстановился»). board задан явно —
    уважаем его, резолвим только тикер."""
    from services.market_data import MarketDataService
    secid, primary = await MarketDataService.resolve_secid_board(isin)
    return secid or isin, (board or primary or "TQCB")


async def load_backdate_ctx(isin: str, d: date, board: Optional[str] = None) -> dict:
    """Тёплый as-of контекст: статика бумаги + факт-строка MOEX history на D +
    кривая as-of. Далее reprice_asof(ctx, price) — без I/O (батчится по ценам)."""
    from datetime import date as _date
    from services.market_data import MarketDataService
    from services.bonds import create_bond_ref_data, build_ref_external
    from services.paths import cache_path as _cache_path
    from services.valuation import _index_provider

    if d >= _date.today():
        raise CalculationException("Дата должна быть в прошлом (для сегодня — обычный калькулятор)")

    cache = MarketDataService.get_local_bond_cache(_cache_path("isins_cache.json"))
    data = cache.get(isin)
    external = data is None
    secid, board = await resolve_market(isin, board)

    async def _aempty():
        return {}

    res = await asyncio.gather(
        MarketDataService.fetch_coupon_schedules([isin]),                             # 0
        MarketDataService.get_curves(),                                               # 1
        MarketDataService.fetch_bond_schedule_full(isin),                             # 2
        fetch_history_row(secid, d, board),                                           # 3
        MarketDataService.fetch_moex_securities([isin]) if external else _aempty(),   # 4
        return_exceptions=True,
    )
    _ok = lambda x, dd: dd if isinstance(x, Exception) else x
    schedules = _ok(res[0], {})
    ruonia_curve, keyrate_curve, _cd, _rd = _ok(res[1], (None, None, None, None))
    sched_full = _ok(res[2], {"coupons": [], "amorts": []})
    hist_row = _ok(res[3], None)
    mo_map = _ok(res[4], {})

    if data:
        ref_obj = create_bond_ref_data(data, isin)
    else:
        mo = mo_map.get(isin, {})
        if not mo:
            raise NotFoundException(f"Bond {isin} not found on MOEX", {"isin": isin})
        ref_obj = build_ref_external(isin, mo)

    if ref_obj.maturity_date and ref_obj.maturity_date <= d:
        raise CalculationException(f"{isin} погашена до {d.isoformat()}")
    if ref_obj.issue_date and ref_obj.issue_date > d:
        raise CalculationException(f"{isin} размещена после {d.isoformat()}")

    today_curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    if today_curve is None:
        raise CalculationException("Текущая кривая недоступна — as-of сшить не из чего")

    warnings: list = []
    _index_pct_fn, hist_pairs = _index_provider(ref_obj.base, warnings, None)
    if not hist_pairs:
        raise CalculationException(f"история {ref_obj.base} недоступна")

    curve, curve_mode = curve_asof(ref_obj.base, d, today_curve, hist_pairs)
    # база Y-IDX для всех флоатеров — роллирование RUONIA, тоже as-of этой даты
    if ref_obj.base == "RUONIA":
        ru_curve_asof = curve
    else:
        _ru_hist = _index_provider("RUONIA", warnings, None)[1]
        ru_curve_asof = (curve_asof("RUONIA", d, ruonia_curve, _ru_hist)[0]
                         if (ruonia_curve is not None and _ru_hist) else None)
        if ru_curve_asof is None:
            warnings.append("RUONIA-кривая на дату не восстановлена — Y-IDX не посчитан")

    periods = schedules.get(isin) or schedules.get(secid)
    amorts = sched_full.get("amorts")
    offers = sched_full.get("offers")
    if not periods:
        # без расписания ядро строит сетку купонов сама и прогнозирует ВСЕ купоны,
        # включая уже зафиксированный текущий — раньше это происходило молча
        # (сбой ISS / бумага без bondization), и цифра расходилась с паспортом
        warnings.append("расписание купонов MOEX не получено — даты купонов сгенерированы, "
                        "фактические (уже объявленные) суммы купонов НЕ использованы")

    # дата факт-строки: MOEX отдаёт последний ТОРГОВЫЙ день ≤ D (выходные/праздники
    # и неликвид без сделок) — по ней решаем, доначислять ли НКД/пересчитывать номинал
    try:
        row_date = date.fromisoformat(hist_row["date"]) if (hist_row and hist_row.get("date")) else None
    except (TypeError, ValueError):
        row_date = None
    stale_days = (d - row_date).days if row_date else None

    # номинал на D: факт биржи; на несвежей строке и при её отсутствии — остаток
    # по графику амортизаций именно на D (транш мог пройти после row_date)
    from services.bonds import amort_remaining_face
    face_asof = hist_row.get("facevalue") if hist_row else None
    if face_asof is None or (stale_days or 0) > 0:
        face_by_sched = amort_remaining_face(amorts, d)
        if face_by_sched is not None:
            if face_asof is not None and abs(face_by_sched - face_asof) > 0.5:
                warnings.append(
                    f"номинал на {d.isoformat()} взят из графика амортизаций ({face_by_sched:g}₽): "
                    f"биржевая строка от {row_date.isoformat()} даёт {face_asof:g}₽")
            face_asof = face_by_sched
    if face_asof is not None and abs(face_asof - ref_obj.face_value) > 0.5:
        ref_obj.face_value = float(face_asof)

    # НКД на D: факт биржи (доначисленный до D, если торгов в этот день не было);
    # фолбэки — график купонов, затем начисление по конвенции выпуска
    accrued_asof = hist_row.get("accint") if hist_row else None
    if accrued_asof is not None and stale_days:
        accrued_asof, note = _accrue_to_date(float(accrued_asof), row_date, d,
                                             periods, ref_obj.face_value)
        if note:
            warnings.append(note)
    if accrued_asof is None:
        accrued_asof = _accrued_from_periods(periods, d, ref_obj.face_value)
        if accrued_asof is not None:
            warnings.append("НКД на дату оценён из графика купонов (MOEX history не отдал ACCINT)")
    if accrued_asof is None:
        idx_at_d = None
        for _md, _r in hist_pairs:          # факт индекса ЦБ на D (история отсортирована)
            if _md <= d:
                idx_at_d = _r
            else:
                break
        accrued_asof = _accrued_estimate(periods, d, ref_obj.face_value, idx_at_d,
                                         ref_obj.spread_issue_bps)
        if accrued_asof is not None:
            warnings.append("НКД на дату начислен по конвенции выпуска (индекс+маржа): "
                            "ни биржевого ACCINT, ни опубликованного купона периода нет")
    if accrued_asof is None:
        raise CalculationException(f"НКД на {d.isoformat()} не восстановился")

    if curve_mode == "realized":
        warnings.append("кривая as-of: реализованный факт индекса + ближайшая архивная "
                        "своп-кривая дальше (архив котировок дату не покрывает)")

    return {
        "isin": isin,
        "date": d,
        "ref_obj": ref_obj,
        "curve": curve,
        "ruonia_curve": ru_curve_asof,
        "curve_mode": curve_mode,
        "accrued": float(accrued_asof),
        "close": hist_row.get("close") if hist_row else None,
        "legalclose": hist_row.get("legalclose") if hist_row else None,
        "trade_date": hist_row.get("date") if hist_row else None,
        "stale_days": stale_days,
        "secid": secid,
        "board": board,
        "periods": periods,
        "amorts": amorts,
        "offers": offers,
        "ctx_warnings": warnings,
    }


async def fetch_history_range(secid: str, d_from: date, d_till: date,
                              board: str = "TQCB") -> list:
    """Все дневные строки MOEX history за диапазон (пагинация start=): по датам
    возрастания, [{date, close, legalclose, accint, facevalue}, ...]."""
    url = (f"https://iss.moex.com/iss/history/engines/stock/markets/bonds/"
           f"boards/{board}/securities/{secid}.json")
    out, start = [], 0
    try:
        async with httpx.AsyncClient() as client:
            while True:
                params = {
                    "iss.meta": "off", "start": start,
                    "from": d_from.isoformat(), "till": d_till.isoformat(),
                    "history.columns": "TRADEDATE,CLOSE,LEGALCLOSEPRICE,ACCINT,FACEVALUE",
                }
                resp = await client.get(url, params=params, timeout=10)
                if resp.status_code != 200:
                    break
                h = resp.json().get("history", {})
                cols, data = h.get("columns", []), h.get("data", [])
                if not data:
                    break
                idx = {c: i for i, c in enumerate(cols)}
                for row in data:
                    g = lambda c: row[idx[c]] if c in idx else None
                    out.append({"date": g("TRADEDATE"), "close": g("CLOSE"),
                                "legalclose": g("LEGALCLOSEPRICE"),
                                "accint": g("ACCINT"), "facevalue": g("FACEVALUE")})
                if len(data) < 100:
                    break
                start += len(data)
    except Exception as e:
        logger.warning(f"MOEX history range fetch {secid}: {e}")
    return out


async def asof_bar_metrics(isin: str, days: int, board: Optional[str] = None):
    """Sync-фабрика ЧЕСТНЫХ метрик для прошлых дней: fn(day_iso, price_pct) →
    {y_idx_bps, dm_bps, g_spread_bps, yield_pct}. Кривая/НКД/номинал — as-of
    дня (та же математика, что honest_spread_series), но прайсит ЛЮБУЮ цену дня
    (часовые бары: vwap/OHLC), а не только close. I/O — только здесь, при сборке;
    сама fn — чистый CPU, можно звать из heavy-потока. Поднимает исключение,
    если as-of контекст не собрался (бумага погашена/только размещена/нет кривой)."""
    from datetime import date as _date, timedelta as _td
    d_till = _date.today() - _td(days=1)
    d_from = d_till - _td(days=days + 7)
    ctx = await load_backdate_ctx(isin, d_till, board)
    rows = await fetch_history_range(ctx["secid"], d_from, d_till, ctx["board"])
    ref = ctx["ref_obj"]
    periods, amorts, offers = ctx["periods"], ctx["amorts"], ctx["offers"]

    from services.valuation import _index_provider, calculate_valuation_metrics
    from services.market_data import MarketDataService
    warns: list = []
    _fn, hist_pairs = _index_provider(ref.base, warns, None)
    _r, _k, _cd, _rd = await MarketDataService.get_curves()
    today_curve = _r if ref.base == "RUONIA" else _k
    if today_curve is None:
        raise CalculationException("Текущая кривая недоступна — as-of сшить не из чего")
    ru_hist = hist_pairs if ref.base == "RUONIA" else _index_provider("RUONIA", warns, None)[1]

    dates = [r["date"] for r in rows]
    curve_memo: dict = {}

    def fn(day_iso: str, price: float) -> dict:
        d = _date.fromisoformat(day_iso)
        i = bisect_right(dates, day_iso) - 1
        row = rows[i] if i >= 0 else None
        accint = row.get("accint") if row else None
        if accint is not None and row["date"] != day_iso:
            # день без строки history (выходная сессия) — доначисляем от факта
            accint, _n = _accrue_to_date(float(accint), _date.fromisoformat(row["date"]),
                                         d, periods, ref.face_value)
        if accint is None:
            accint = _accrued_from_periods(periods, d, ref.face_value)
        if accint is None:
            return {}
        if row and row.get("facevalue"):
            ref.face_value = float(row["facevalue"])
        cm = curve_memo.get(day_iso)
        if cm is None:
            curve, _mode = curve_asof(ref.base, d, today_curve, hist_pairs)
            ru = (curve if ref.base == "RUONIA" else
                  (curve_asof("RUONIA", d, _r, ru_hist)[0]
                   if (_r is not None and ru_hist) else None))
            cm = curve_memo[day_iso] = (curve, ru)
        curve, ru = cm
        m = calculate_valuation_metrics(
            ref, price, curve, d, accrued_override=float(accint),
            periods=periods, amorts=amorts, offers=offers,
            ruonia_curve=ru, accrued_basis="calc")
        return {"y_idx_bps": m.get("yield_over_index_bps"),
                "dm_bps": m.get("disc_margin_bps"),
                "g_spread_bps": m.get("g_spread_bps"),
                "yield_pct": m.get("yield_xirr_pct")}

    return fn


_honest_memo: dict = {}     # (isin, days, board) → (msk_day, result); прошлое не меняется,
                            # хвост realized-кривой обновляется раз в день — TTL сутки


async def honest_spread_series(isin: str, days: int = 180, board: Optional[str] = None,
                               price_overrides: Optional[dict] = None) -> dict:
    """Честная динамика спредов: для КАЖДОГО торгового дня — свой calc_date,
    своя as-of кривая, фактические НКД/номинал/close того дня → SM/DM/y-idx.
    В отличие от candle-оценки (историч. цена × сегодняшняя модель) серия не
    зависит от сегодняшних НКД/срока; хвост кривой за «сегодня» — текущий рынок.
    price_overrides {date_iso: price} — для даты считать на этой цене, не на close
    (бэкфилл легаси-снапшотов на их же цене). Расчёт ~15с на 120 дней → мемо на
    день (только без overrides)."""
    from datetime import date as _date, timedelta as _td
    key = (isin, days, board)
    hit = None if price_overrides else _honest_memo.get(key)
    if hit and hit[0] == _date.today():
        return hit[1]
    d_till = _date.today() - _td(days=1)
    d_from = d_till - _td(days=int(days * 1.55) + 7)   # запас на выходные

    ctx = await load_backdate_ctx(isin, d_till, board)  # статика + кривая на d_till
    rows = await fetch_history_range(ctx["secid"], d_from, d_till, ctx["board"])
    # цена дня: CLOSE, при её отсутствии — официальный LEGALCLOSEPRICE (у неликвида
    # сделок может не быть, но оценочная цена биржей публикуется)
    for r in rows:
        if r.get("close") is None and r.get("legalclose") is not None:
            r["close"] = r["legalclose"]
    rows = [r for r in rows if r.get("close") is not None][-days:]

    from services.valuation import calculate_valuation_metrics
    ref = ctx["ref_obj"]
    from services.valuation import _index_provider
    warnings: list = []
    _fn, hist_pairs = _index_provider(ref.base, warnings, None)
    from services.market_data import MarketDataService
    _r, _k, _cd, _rd = await MarketDataService.get_curves()
    today_curve = _r if ref.base == "RUONIA" else _k
    # база Y-IDX — роллирование RUONIA и для КС-бумаг: нужна RUONIA-кривая НА ТУ ЖЕ
    # прошлую дату (сегодняшняя дала бы завтрашние ожидания во вчерашней цифре)
    ru_hist = hist_pairs if ref.base == "RUONIA" else _index_provider("RUONIA", warnings, None)[1]

    points = []
    for r in rows:
        try:
            d = _date.fromisoformat(r["date"])
        except (TypeError, ValueError):
            continue
        try:
            curve, mode = curve_asof(ref.base, d, today_curve, hist_pairs)
            ru_curve = curve if ref.base == "RUONIA" else curve_asof("RUONIA", d, _r, ru_hist)[0]
            if r.get("facevalue"):
                ref.face_value = float(r["facevalue"])
            accint = r.get("accint")
            if accint is None:
                accint = _accrued_from_periods(ctx["periods"], d, ref.face_value)
            if accint is None:
                # accrued_override=None → движок взял бы bond.accrued_rub, т.е. НКД
                # СЕГОДНЯШНЕГО дня на прошлую дату (тихий мусор в серии). Точку рвём.
                logger.debug(f"honest point {isin}@{r['date']}: НКД на дату не восстановился")
                continue
            px = (price_overrides or {}).get(r["date"], r["close"])
            m = calculate_valuation_metrics(
                ref, px, curve, d, accrued_override=accint,
                periods=ctx["periods"], amorts=ctx["amorts"], offers=ctx["offers"],
                ruonia_curve=ru_curve, accrued_basis="calc")
            points.append({
                "date": r["date"], "price": px,
                "sm_bps": m.get("sm_bps"), "dm_bps": m.get("disc_margin_bps"),
                "ytm": m.get("yield_xirr_pct"),
                "y_idx_bps": m.get("yield_over_index_bps"),
                "curve_mode": mode, "src": "honest",
            })
        except Exception as e:
            logger.debug(f"honest point {isin}@{r['date']}: {e}")
    result = {"isin": isin, "points": points, "warnings": ctx["ctx_warnings"]}
    if not price_overrides:
        _honest_memo[key] = (_date.today(), result)
    return result


_backfill_done: dict = {}   # (isin, board) → (msk_day, days) — бэкфилл уже сделан сегодня

# Версия as-of движка. ПОДНЯТЬ при любой правке, меняющей цифру на прошлую дату
# (НКД/номинал/купон/кривая) — honest-строки прошлых версий будут снесены и
# пересчитаны при первом открытии графика бумаги.
#   1 — базовая (до 2026-08-03)
#   2 — 2026-08-03: НКД доначисляется до нерабочей даты, ОФЗ резолвятся в
#       SECID/TQOB (были без фактических купонов), убран тихий фолбэк на
#       сегодняшний НКД, цена дня падает на legalclose при отсутствии сделок
#   3 — 2026-08-04: база Y-IDX — роллирование RUONIA для ВСЕХ флоатеров
#       (КС-бумаги больше не сравниваются с квартальным роллированием КС),
#       компаундинг по рабочим дням + простое начисление на нерабочих
#   4 — 2026-08-11: якорь realized-гибрида — ПЕРВАЯ архивная кривая, не
#       сегодняшняя: серия рвалась скачком на границе архива котировок
#       (realized→market), у ТрансмхПБ8 −50bp за день при флэт цене
HONEST_ENGINE_VERSION = 4


async def ensure_honest_backfill(isin: str, days: int, board: Optional[str] = None) -> int:
    """Разово досчитывает честную историю в spread_daily: даты без строки —
    INSERT (src='honest', цена=close), легаси-снапшоты без y_idx — пересчёт
    НА ИХ ЦЕНЕ (price_pct) → UPDATE y_idx. Строки старых версий движка сносятся
    и считаются заново. Прошлое зафиксировано в базе — дальше читается мгновенно;
    повторный вызов за день (и на меньший период) no-op. Возвращает число
    записанных/обновлённых строк."""
    from datetime import date as _date
    done = _backfill_done.get((isin, board))
    if done and done[0] == _date.today() and done[1] >= days:
        return 0

    from services.spread_history import read_history, upsert_honest, drop_stale_honest
    dropped = drop_stale_honest(isin, HONEST_ENGINE_VERSION)
    if dropped:
        logger.info("honest %s: снесено %d строк старого движка → пересчёт", isin, dropped)
    existing = {r["date"]: r for r in read_history(isin, days=days + 10)
                if (r.get("kind") or "floater") == "floater"}
    # Доверенные источники — вечерний снапшот ('snap', живой движок в свой день)
    # и honest-бэкфилл текущей версии. Строки БЕЗ src — легаси candle-est
    # (scripts/backfill_yidx_history.py: цена дня × модель на день прогона):
    # их y_idx/dm недоверенные, пересчитываются честно на их же цене.
    _trusted = lambda r: r.get("src") in ("snap", "honest")
    untrusted = {d for d, r in existing.items() if not _trusted(r)}
    overrides = {d: r["price_pct"] for d, r in existing.items()
                 if r.get("price_pct") is not None
                 and (d in untrusted or r.get("y_idx") is None)}
    # окно уже покрыто доверенными строками с y_idx и дыр-легаси нет →
    # тяжёлый пересчёт не нужен
    have = sum(1 for d, r in existing.items()
               if _trusted(r) and r.get("y_idx") is not None)
    if not overrides and have >= days:
        _backfill_done[(isin, board)] = (_date.today(), days)
        return 0
    series = await honest_spread_series(isin, days, board,
                                        price_overrides=overrides or None)
    missing_or_null = [p for p in series["points"]
                       if p["date"] not in existing or p["date"] in overrides]
    n = (upsert_honest(isin, missing_or_null, set(existing), HONEST_ENGINE_VERSION,
                       retrust_dates=untrusted)
         if missing_or_null else 0)
    # недоверенные даты, до которых честный движок не дотянулся (нет строки
    # MOEX history — выходные сессии/дыры) — сносим, иначе мусор рисуется вечно
    # пустая серия — скорее сбой MOEX/сети, чем «дат нет»: не сносим, дождёмся
    if untrusted and series["points"]:
        from services.spread_history import drop_untrusted
        # покрытой считается дата с ХОТЬ ОДНОЙ метрикой: точка с y_idx=dm=None
        # (солвер споткнулся, напр. день выплаты купона) строку не перезаписывает
        # и без этого фильтра оставляла бы легаси-мусор жить вечно
        covered = {p["date"] for p in series["points"]
                   if p.get("y_idx_bps") is not None or p.get("dm_bps") is not None}
        n += drop_untrusted(isin, untrusted - covered)
    _backfill_done[(isin, board)] = (_date.today(), days)
    return n + dropped


def reprice_asof(ctx: dict, price: float) -> dict:
    """Чистая цена на прошлую дату → полный набор метрик (SM/DM/y-idx/YTM/dirty).
    Тот же движок, что live-таблица; calc_date = прошлая дата, входы as-of."""
    from services.valuation import calculate_valuation_metrics
    m = calculate_valuation_metrics(
        ctx["ref_obj"], price, ctx["curve"], ctx["date"],
        accrued_override=ctx["accrued"], periods=ctx["periods"],
        amorts=ctx["amorts"], offers=ctx["offers"],
        ruonia_curve=ctx.get("ruonia_curve"),
        # history-ACCINT (и фолбэк из графика) — НКД на дату торгов, не на поставку
        accrued_basis="calc",
    )
    m["warnings"] = sorted(set((m.get("warnings") or []) + ctx["ctx_warnings"]))
    m["curve_mode"] = ctx["curve_mode"]
    return m
