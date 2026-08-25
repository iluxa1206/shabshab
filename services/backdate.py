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
from collections import OrderedDict
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
                 dates: list, rates: list, index_levels: dict = None):
        self.base_type = base
        # накопленный индекс ЦБ на факт-сегменте — эталон роста o/n-роллирования
        # (см. realized_growth); None — эталона нет, потребитель реконструирует
        self._index = index_levels or None
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

    def realized_until(self) -> date:
        """Факт кончается на стыке с anchor: до splice — реализованный индекс."""
        return self._splice

    def realized_growth(self, t1: date, t2: date):
        """Рост o/n-роллирования по официальному накопленному индексу ЦБ.

        Отношение уровней — ровно то, что ЦБ уже посчитал своей же механикой
        (капитализация в рабочие дни, простое начисление на нерабочих, ACT/ACT).
        None — индекс не передан, отрезок вне факта или дат нет в истории; тогда
        потребитель честно откатывается на реконструкцию по ставкам."""
        if not self._index or t2 <= t1:
            return None
        if t1 < self.calc_date or t2 > self._splice:
            return None
        a, b = self._index.get(t1), self._index.get(t2)
        if not a or not b or a <= 0.0:
            return None
        return b / a

    def rate_bounds(self) -> list:
        """Ступень гибрида ДНЕВНАЯ до splice (факт индекса ЦБ), дальше — ступень
        anchor. Узлов у гибрида всего два (eff, splice), поэтому дефолтная
        реализация DiscountCurve.rate_bounds отдала бы одну границу на весь
        факт-сегмент, и потребитель (путь роллирования RUONIA) заморозил бы
        уровень первого дня до самого splice."""
        lo = bisect_right(self._dates, self.calc_date)
        hi = bisect_right(self._dates, self._splice)
        out = [d for d in self._dates[lo:hi] if d < self._splice]
        out.append(self._splice)
        ab = getattr(self._anchor, "rate_bounds", None)
        if callable(ab):
            out.extend(d for d in ab() if d > self._splice)
        return out

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


_ru_index_memo: dict = {}     # день → {дата: уровень}; история прошлого не меняется


def _ruonia_index_levels() -> Optional[dict]:
    """Официальный накопленный индекс RUONIA как {дата: уровень}, дневной кэш.

    Только для RUONIA: у КС такого индекса нет (это ставка ЦБ, не индекс), а база
    Y-IDX для всех флоатеров и так RUONIA. Сбой источника — None, гибрид просто
    остаётся на реконструкции по ставкам."""
    today = date.today()
    hit = _ru_index_memo.get(today)
    if hit is not None:
        return hit or None
    try:
        from services import cbr
        rows = cbr.ruonia_index_history()
        levels = {d: ix for d, ix, _ in rows if ix}
    except Exception as e:
        logger.warning(f"RUONIA index недоступен ({e}) — рост роллирования реконструируем")
        levels = {}
    _ru_index_memo.clear()
    _ru_index_memo[today] = levels
    return levels or None


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

    return SplicedAsofCurve(base, eff, splice, today_curve, dates, rates,
                            index_levels=_ruonia_index_levels() if base == "RUONIA" else None)


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
    """ЛЕГАСИ, оставлено для тестов: «индекс на дату + маржа», simple ACT/365.

    В расчёте больше не используется — это последняя ступень общей лестницы
    services/accrued.accrued_for, и вызывается она оттуда. Отдельно этот путь
    завышал НКД вдвое при падающей ставке, потому что берёт СПОТ индекса вместо
    ставки периода по спеке."""
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
    if p_d is not None:
        # период сменился, а купон НОВОГО периода ещё не опубликован (value=None,
        # обычный случай флоатера в день выплаты). Оставить факт нельзя: это НКД
        # СТАРОГО периода почти в полный купон — dirty завышался на купон, и
        # ex-coupon ветка accrue_to_settle уже не срабатывала (calc и settle
        # лежат в одном новом периоде) → YTM проваливался в ноль
        # (ФосАгро П2 @ 2026-08-09: 11.74₽ вместо ~0.4₽, R-spread −1453bps).
        s2 = p_d[0]
        k = (d - s2).days
        if k <= 0:
            return 0.0, note + " (день выплаты купона: новый период начался, НКД обнулён)"
        if p_t is not None:
            sc, ec, vc = p_t
            if vc is not None:
                daily = float(vc) / ((ec - sc).days or 1)
            elif (trade_date - sc).days > 0:
                daily = accint_fact / (trade_date - sc).days
            else:
                daily = None
            if daily is not None:
                return round(daily * k, 4), note + (
                    " (в промежутке выплата, новый купон не опубликован — "
                    "начислено по дневной ставке прошлого купона)")
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
            warnings.append("RUONIA-кривая на дату не восстановлена — R-spread не посчитан")

    periods = schedules.get(isin) or schedules.get(secid)
    amorts = sched_full.get("amorts")
    # Даты колла подмешаны в общий дневной кэш расписаний на СЕГОДНЯ — для as-of
    # расчёта пересобираем их на дату d (у бермудского колла даты каждый месяц,
    # иначе горизонт брался бы из будущего относительно расчётной даты).
    from services.market_data import call_offers_asof
    offers = call_offers_asof(isin, sched_full.get("offers"), d)
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
        face_by_sched = amort_remaining_face(amorts, d, ref_obj.face_value)
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
        # единая лестница источников (services/accrued): спека фиксинга →
        # прошлый купон → индекс+маржа. Своя реализация «спот-индекс + маржа»
        # завышала вдвое (ВЭБ2Р-50 25.08: 42 ₽ против биржевых 22,7)
        from services.accrued import accrued_for
        accrued_asof, how = accrued_for(
            periods, d, face=ref_obj.face_value, base=ref_obj.base,
            margin_bps=ref_obj.spread_issue_bps, isin=isin,
            index_pct=idx_at_d, calc_date=d)
        if accrued_asof is not None:
            warnings.append(f"НКД на дату оценён ({how}): ни биржевого ACCINT, "
                            "ни опубликованного купона периода нет")
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


# Дневная история MOEX по бумаге: кэш на день. Она НЕИЗМЕННА (прошлые торговые
# дни не переписываются), а стоит дорого — окно 730 дней это ~8 последовательных
# страниц ISS, и запрашивают её три места сразу (as-of фабрика баров, honest-
# серия, контекст бэкдейта). Хранится САМОЕ ШИРОКОЕ окно на бумагу: запрос
# поуже отдаётся срезом, без единого похода в сеть.
_HIST_MEMO_MAX = 200
_hist_memo: "OrderedDict[tuple, tuple]" = OrderedDict()   # (secid,board) → (день, frm, till, rows)


def _hist_memo_get(secid: str, board: str, d_from: date, d_till: date) -> Optional[list]:
    from datetime import date as _date
    hit = _hist_memo.get((secid, board))
    if not hit or hit[0] != _date.today():
        return None
    _day, frm, till, rows = hit
    if frm > d_from or till < d_till:
        return None                      # кэш у́же запроса — досчитывать нечем
    _hist_memo.move_to_end((secid, board))
    a, b = d_from.isoformat(), d_till.isoformat()
    return [r for r in rows if a <= (r.get("date") or "") <= b]


def _hist_memo_put(secid: str, board: str, d_from: date, d_till: date, rows: list) -> None:
    from datetime import date as _date
    key = (secid, board)
    prev = _hist_memo.get(key)
    if prev and prev[0] == _date.today() and prev[1] <= d_from and prev[2] >= d_till:
        return                           # в кэше уже более широкое окно
    _hist_memo[key] = (_date.today(), d_from, d_till, rows)
    _hist_memo.move_to_end(key)
    for k in [k for k, v in list(_hist_memo.items()) if v[0] != _date.today()]:
        _hist_memo.pop(k, None)
    while len(_hist_memo) > _HIST_MEMO_MAX:
        _hist_memo.popitem(last=False)


async def fetch_history_range(secid: str, d_from: date, d_till: date,
                              board: str = "TQCB") -> list:
    """Все дневные строки MOEX history за диапазон (пагинация start=): по датам
    возрастания, [{date, close, legalclose, accint, facevalue}, ...].
    Кэш на день (см. _hist_memo): прошлые торговые дни не меняются."""
    cached = _hist_memo_get(secid, board, d_from, d_till)
    if cached is not None:
        return cached
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
    # пустой ответ не кэшируем: это сбой сети, а не «истории нет» — иначе один
    # флак ISS замораживал бы бумагу без спреда на весь день (см. ВЭБP-41)
    if out:
        _hist_memo_put(secid, board, d_from, d_till, out)
    return out


def _alt_horizon(hz_key: str, horizons: dict) -> Optional[str]:
    """Обёртка над общим valuation.alt_horizon (импорт ленивый — как всё, что
    этот модуль берёт из valuation: там свои тяжёлые зависимости)."""
    from services.valuation import alt_horizon
    return alt_horizon(hz_key, horizons)


# Собранная as-of фабрика: кэш на день по (isin, board) с ЗАПОМНЕННЫМ окном.
# Сборка — это сеть (история, кривые) плюс контексты по дням; её просят и бары,
# и маркеры сделок, и стакан, причём с разными окнами. Фабрика умеет любую дату
# внутри своего окна, поэтому запрос поуже обслуживается уже построенной.
_ASOF_MEMO_MAX = 120
_asof_memo: "OrderedDict[tuple, tuple]" = OrderedDict()   # (isin,board) → (день, days, fn)


async def asof_bar_metrics(isin: str, days: int, board: Optional[str] = None):
    """Sync-фабрика ЧЕСТНЫХ метрик для прошлых дней: fn(day_iso, price_pct) →
    {y_idx_bps, dm_bps, g_spread_bps, yield_pct}. Кривая/НКД/номинал — as-of
    дня (та же математика, что honest_spread_series), но прайсит ЛЮБУЮ цену дня
    (часовые бары: vwap/OHLC), а не только close. I/O — только здесь, при сборке;
    сама fn — чистый CPU, можно звать из heavy-потока. Поднимает исключение,
    если as-of контекст не собрался (бумага погашена/только размещена/нет кривой).

    Готовая фабрика кэшируется на день (_asof_memo): её строят и бары, и маркеры
    сделок, и honest-серия — каждый раз заново это была сеть плюс контексты по
    дням (на холодную до сотни секунд на длинном окне)."""
    from datetime import date as _date, timedelta as _td
    mkey = (isin, board)
    hit = _asof_memo.get(mkey)
    if hit and hit[0] == _date.today() and hit[1] >= days:
        _asof_memo.move_to_end(mkey)     # окно кэша шире запроса — годится
        return hit[2]
    d_till = _date.today() - _td(days=1)
    d_from = d_till - _td(days=days + 7)
    ctx = await load_backdate_ctx(isin, d_till, board)
    rows = await fetch_history_range(ctx["secid"], d_from, d_till, ctx["board"])
    ref = ctx["ref_obj"]
    periods, amorts, offers = ctx["periods"], ctx["amorts"], ctx["offers"]

    from services.valuation import _index_provider, calculate_valuation_metrics, pick_horizon
    from services.market_data import MarketDataService
    warns: list = []
    _fn, hist_pairs = _index_provider(ref.base, warns, None)
    _r, _k, _cd, _rd = await MarketDataService.get_curves()
    today_curve = _r if ref.base == "RUONIA" else _k
    if today_curve is None:
        raise CalculationException("Текущая кривая недоступна — as-of сшить не из чего")
    ru_hist = hist_pairs if ref.base == "RUONIA" else _index_provider("RUONIA", warns, None)[1]

    # ПУСТАЯ ИСТОРИЯ = фабрика не собралась, а не «спреда нет». Раньше она молча
    # возвращала fn, отвечающую {} на любую дату: при флаке ISS (одна оборванная
    # выборка) бары писались БЕЗ спреда, но со штампом текущей версии — и
    # считались посчитанными навсегда. ВЭБP-41: 01-13.08 без единой точки, при
    # том что модель и история в порядке.
    if not rows:
        raise CalculationException(
            f"история MOEX за окно пуста ({isin}) — as-of считать не из чего")

    dates = [r["date"] for r in rows]
    curve_memo: dict = {}
    # ГОРИЗОНТ ФИКСИРУЕМ НА ВСЮ СЕРИЮ, по последней цене окна. Пересчитывать
    # правило цены на каждый день нельзя: у бумаги с офертой около номинала
    # горизонт скакал бы put↔maturity от дня ко дню, а это разные метрики
    # (РЖД 1Р-52R при 99.0: put 165 б.п. против maturity 83) — линия рвалась бы
    # ступенями там, где рынок стоял на месте. Один горизонт на серию = то же
    # число, что в шапке и стакане сегодня, и однородная история.
    hz_key, alt_key = "maturity", None
    _last = next((r for r in reversed(rows) if r.get("close") is not None), None)
    if _last is not None:
        try:
            _c, _mode = curve_asof(ref.base, _date.fromisoformat(_last["date"]),
                                   today_curve, hist_pairs)
            _ru = (_c if ref.base == "RUONIA" else
                   (curve_asof("RUONIA", _date.fromisoformat(_last["date"]), _r, ru_hist)[0]
                    if (_r is not None and ru_hist) else None))
            _m = calculate_valuation_metrics(
                ref, float(_last["close"]), _c, _date.fromisoformat(_last["date"]),
                accrued_override=(float(_last["accint"]) if _last.get("accint") is not None else None),
                periods=periods, amorts=amorts, offers=offers,
                ruonia_curve=_ru, accrued_basis="calc")
            hz_key = _m.get("preferred_horizon") or "maturity"
            alt_key = _alt_horizon(hz_key, _m.get("horizons") or {})
        except Exception as e:
            logger.debug("as-of %s: горизонт по правилу цены не определился (%s)", isin, e)

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
        # ГОРИЗОНТ — по правилу цены, как в карточке, стакане и ленте сделок.
        # Верхнеуровневые поля ответа всегда к погашению: у бумаги с офертой
        # (РЖД 1Р-52R: put 09.10.2029 при погашении 31.03.2036) линия графика
        # считалась к 2036-му, а R-spread в шапке — к оферте, и одна и та же
        # метрика на одном экране расходилась на сотни б.п.
        h = pick_horizon(m, hz_key)
        # ВТОРОЙ ГОРИЗОНТ считаем тем же прогоном и кладём рядом: свитчер
        # «к погашению / к оферте» на графике должен переключаться мгновенно,
        # а пересчёт года истории по требованию — это минуты.
        alt = pick_horizon(m, alt_key) if alt_key else {}
        return {"y_idx_bps": h.get("yield_over_index_bps", m.get("yield_over_index_bps")),
                "dm_bps": h.get("disc_margin_bps", m.get("disc_margin_bps")),
                "g_spread_bps": m.get("g_spread_bps"),
                "yield_pct": h.get("yield_xirr_pct", m.get("yield_xirr_pct")),
                "horizon": h.get("horizon"),
                "y_idx_alt_bps": alt.get("yield_over_index_bps"),
                "alt_horizon": alt.get("horizon") if alt else None}

    _asof_memo[mkey] = (_date.today(), days, fn)
    _asof_memo.move_to_end(mkey)
    for k in [k for k, v in list(_asof_memo.items()) if v[0] != _date.today()]:
        _asof_memo.pop(k, None)
    while len(_asof_memo) > _ASOF_MEMO_MAX:
        _asof_memo.popitem(last=False)
    return fn


# (isin, days, board) → (msk_day, result). Прошлое не меняется, хвост realized-
# кривой обновляется раз в день — TTL сутки. РАЗМЕР ОГРАНИЧЕН: в словаре лежат
# полные серии точек (150 дней × dict на день), и за проход по универсу их
# набиралось на сотни мегабайт — процесс за ночь дорастал с 599 МБ до 1 ГБ и
# подходил к лимиту контейнера. Вытесняем самые старые записи (FIFO) и чистим
# вчерашние: серия пересчитывается за секунды из уже готовых строк spread_daily.
_HONEST_MEMO_MAX = 64
_honest_memo: "OrderedDict[tuple, tuple]" = OrderedDict()


def _honest_memo_put(key: tuple, day, result: dict) -> None:
    _honest_memo[key] = (day, result)
    _honest_memo.move_to_end(key)
    # сначала прочь вчерашние (они всё равно невалидны), потом самые старые
    for k in [k for k, (d, _) in list(_honest_memo.items()) if d != day]:
        _honest_memo.pop(k, None)
    while len(_honest_memo) > _HONEST_MEMO_MAX:
        _honest_memo.popitem(last=False)


# Порция потоковой выдачи честной серии (см. on_chunk): ~месяц торгов.
_HONEST_CHUNK = 20


async def honest_spread_series(isin: str, days: int = 180, board: Optional[str] = None,
                               price_overrides: Optional[dict] = None,
                               till: Optional["date"] = None, on_chunk=None) -> dict:
    """Честная динамика спредов: для КАЖДОГО торгового дня — свой calc_date,
    своя as-of кривая, фактические НКД/номинал/close того дня → SM/DM/y-idx.
    В отличие от candle-оценки (историч. цена × сегодняшняя модель) серия не
    зависит от сегодняшних НКД/срока; хвост кривой за «сегодня» — текущий рынок.
    price_overrides {date_iso: price} — для даты считать на этой цене, не на close
    (бэкфилл легаси-снапшотов на их же цене). Расчёт ~15с на 120 дней → мемо на
    день (только без overrides)."""
    from datetime import date as _date, timedelta as _td
    # till — правая граница окна (по умолчанию вчера). Задаётся при досчёте
    # ЛЕВОГО куска расширенного окна: ensure_honest_backfill не пересчитывает
    # то, что уже лежит в базе.
    key = (isin, days, board, till.isoformat() if till else None)
    flushed = [0]                       # сколько точек уже отдано через on_chunk
    hit = None if price_overrides else _honest_memo.get(key)
    if hit and hit[0] == _date.today():
        _honest_memo.move_to_end(key)      # свежеиспользованное вытесняется последним
        return hit[1]
    d_till = till or (_date.today() - _td(days=1))
    d_from = d_till - _td(days=int(days * 1.55) + 7)   # запас на выходные

    ctx = await load_backdate_ctx(isin, d_till, board)  # статика + кривая на d_till
    rows = await fetch_history_range(ctx["secid"], d_from, d_till, ctx["board"])
    # цена дня: CLOSE, при её отсутствии — официальный LEGALCLOSEPRICE (у неликвида
    # сделок может не быть, но оценочная цена биржей публикуется)
    for r in rows:
        if r.get("close") is None and r.get("legalclose") is not None:
            r["close"] = r["legalclose"]
    rows = [r for r in rows if r.get("close") is not None][-days:]

    from services.valuation import calculate_valuation_metrics, pick_horizon
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

    # Горизонт — один на всю серию, по последней цене окна (см. asof_bar_metrics):
    # правило цены, пересчитанное на каждый день, ломало бы линию ступенями у
    # бумаг с офертой около номинала.
    hz_key, alt_key = "maturity", None
    if rows:
        try:
            _d = _date.fromisoformat(rows[-1]["date"])
            _c, _mode = curve_asof(ref.base, _d, today_curve, hist_pairs)
            _ru = _c if ref.base == "RUONIA" else curve_asof("RUONIA", _d, _r, ru_hist)[0]
            _acc = rows[-1].get("accint")
            if _acc is None:
                _acc = _accrued_from_periods(ctx["periods"], _d, ref.face_value)
            _m = calculate_valuation_metrics(
                ref, rows[-1]["close"], _c, _d, accrued_override=_acc,
                periods=ctx["periods"], amorts=ctx["amorts"], offers=ctx["offers"],
                ruonia_curve=_ru, accrued_basis="calc")
            hz_key = _m.get("preferred_horizon") or "maturity"
            alt_key = _alt_horizon(hz_key, _m.get("horizons") or {})
        except Exception as e:
            logger.debug("honest %s: горизонт по правилу цены не определился (%s)", isin, e)

    points = []
    # ОТ СВЕЖИХ К СТАРЫМ при потоковой выдаче (on_chunk): правый край графика
    # человек видит первым, туда и должны ложиться первые посчитанные точки.
    for r in (reversed(rows) if on_chunk is not None else rows):
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
            hz = pick_horizon(m, hz_key)     # см. asof_bar_metrics: один горизонт на серию
            alt = pick_horizon(m, alt_key) if alt_key else {}
            points.append({
                "date": r["date"], "price": px,
                "sm_bps": hz.get("sm_bps", m.get("sm_bps")),
                "dm_bps": hz.get("disc_margin_bps", m.get("disc_margin_bps")),
                "ytm": hz.get("yield_xirr_pct", m.get("yield_xirr_pct")),
                "y_idx_bps": hz.get("yield_over_index_bps", m.get("yield_over_index_bps")),
                "y_idx_alt_bps": alt.get("yield_over_index_bps"),
                "curve_mode": mode, "src": "honest", "horizon": hz.get("horizon"),
                "alt_horizon": alt.get("horizon") if alt else None,
            })
            # порция готова — отдаём наружу, не дожидаясь конца окна
            if on_chunk is not None and len(points) - flushed[0] >= _HONEST_CHUNK:
                on_chunk(points[flushed[0]:])
                flushed[0] = len(points)
        except Exception as e:
            logger.debug(f"honest point {isin}@{r['date']}: {e}")
    if on_chunk is not None:
        if len(points) > flushed[0]:
            on_chunk(points[flushed[0]:])
        points.sort(key=lambda p: p["date"])
    result = {"isin": isin, "points": points, "warnings": ctx["ctx_warnings"]}
    if not price_overrides:
        _honest_memo_put(key, _date.today(), result)
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
#   5 — 2026-08-12: НКД в ДЕНЬ ВЫПЛАТЫ купона. На нерабочей дате факт последних
#       торгов (почти полный купон старого периода) оставался НКД новой даты
#       (_accrue_to_date), на рабочей — accrue_to_settle не доначисляла НКД на
#       поставку (calc == старт периода, elapsed=0 → возврат входа), занижая
#       dirty на 1-3 дня накопления
# 6 — 2026-08-13: горизонт по правилу цены (pick_horizon) вместо «всегда к
#     погашению»: у бумаг с офертой история расходилась с шапкой и стаканом.
# 7 — 2026-08-14: неопределённые купоны источника больше не факт. MOEX эхом
#     заполнял хвост будущих купонов последней объявленной ставкой (у старых
#     ОФЗ-ПК — до погашения, 29010: 16.59% до 2034), и весь хвост прайсился
#     замороженной ставкой вместо форварда. Замер на выборке: Y-IDX −5…−212 bps,
#     SM −5…−176 bps — история старого движка несопоставима с новой.
# 8 — 2026-08-20: путь роллирования RUONIA (база Y-IDX) шёл по узлам кривой, а
#     у гибрида as-of узлов два (eff, splice) — уровень ПЕРВОГО ДНЯ замерзал на
#     весь факт-сегмент. База на прошлую дату = спот-индекс, скомпаундированный
#     до погашения, тем сильнее завышенный, чем дальше дата и чем круче с неё
#     ушла ставка. МБЭС 2P-02 @2025-08-20: база 18.01% вместо 16.10%, Y-IDX 48
#     вместо 239 bps при марже выпуска 250. Затронуты ВСЕ realized-даты (до
#     начала архива своп-котировок 2026-07-30); market-даты не менялись.
HONEST_ENGINE_VERSION = 8


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
    # ОКНО УЖЕ ПОКРЫТО? Критерий — САМАЯ РАННЯЯ доверенная дата: если она левее
    # начала окна, считать нечего. Раньше здесь стояло `have >= days` — число
    # доверенных СТРОК против КАЛЕНДАРНЫХ дней окна: торговых дней всегда меньше
    # (в 400-дневном окне их ~270), поэтому условие не выполнялось никогда, и
    # каждый рестарт процесса (memo живёт в памяти) гнал полный пересчёт всей
    # истории заново. Дыры внутри окна повторный прогон не залечит — их там нет
    # ровно потому, что у MOEX нет истории на эти даты.
    from datetime import timedelta as _td2
    have = sum(1 for d, r in existing.items()
               if _trusted(r) and r.get("y_idx") is not None)
    trusted_dates = [d for d, r in existing.items()
                     if _trusted(r) and r.get("y_idx") is not None]
    earliest = min(trusted_dates) if trusted_dates else None
    frm_iso = (_date.today() - _td2(days=days)).isoformat()
    if not overrides and have and earliest and earliest <= frm_iso:
        _backfill_done[(isin, board)] = (_date.today(), days)
        return 0
    # Окно расширили влево — считаем ТОЛЬКО недостающий кусок [frm, earliest),
    # а не всю историю заново (те же грабли, что были у часовых баров).
    till = None
    span = days
    if not overrides and earliest and earliest > frm_iso:
        till = _date.fromisoformat(earliest) - _td2(days=1)
        span = (till - (_date.today() - _td2(days=days))).days + 1
    if span < 1:
        _backfill_done[(isin, board)] = (_date.today(), days)
        return dropped
    # Пишем ПОРЦИЯМИ по ходу счёта: точки появляются на графике по мере расчёта
    # (фронт переспрашивает, пока покрытие неполное), а не все разом в конце —
    # на длинном окне это минуты, в течение которых линия выглядела мёртвой.
    written = [0]
    ex_dates = set(existing)

    def _flush(part: list) -> None:
        fresh = [p for p in part if p["date"] not in existing or p["date"] in overrides]
        if fresh:
            written[0] += upsert_honest(isin, fresh, ex_dates, HONEST_ENGINE_VERSION,
                                        retrust_dates=untrusted)

    series = await honest_spread_series(isin, span, board,
                                        price_overrides=overrides or None,
                                        till=till, on_chunk=_flush)
    n = written[0]
    if not n and series["points"]:
        # серия пришла из memo (её уже считали сегодня) — порций не было,
        # пишем как раньше, одним заходом
        missing_or_null = [p for p in series["points"]
                           if p["date"] not in existing or p["date"] in overrides]
        if missing_or_null:
            n = upsert_honest(isin, missing_or_null, ex_dates, HONEST_ENGINE_VERSION,
                              retrust_dates=untrusted)
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
