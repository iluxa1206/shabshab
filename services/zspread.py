"""
z-спред над КБД ОФЗ (методика НРД) — наш расчётный аналог.

Пайплайн: прогноз купонов на кривой ОЖИДАНИЙ индекса (bootstrap par-свопов) →
дальше по базе:
  RUONIA  — met_rub п.4.9: дисконт по КБД непрерывно exp(-(r_g(τ)+z)τ) → solve z.
  KEYRATE — реверс-инжиниринг публикуемого z НРД (2026-07-02): для флоатеров
    z_НРД ≈ ytm_eff − G(горизонт рефиксинга); их duration = длина купонного
    периода, G клэмпится к короткому концу КБД. Наш аналог:
    y — плоская непрерывная доходность потоков (solve_flat_y), z = (e^y−1) − G(τ_reset).

Валидация (scripts/nrd_pipeline_probe.py, 2026-07-02, цена НРД на входе):
RUONIA ±40bps (старый путь); KEYRATE mean|Δ|≈25bps, медиана −12 (узкая выборка
watch-качества), юниверс 127 бумаг (с амортизируемыми): AAA..A mean|Δ|≈86,
медиана −15 — остаток растёт к низким рейтингам (кросс-секция NSS+Kalman
эмитента у НРД, Прил.6, из публичных данных несводимо). Устойчив во времени
(12 дат: bias ≈ 0, std 17-32bps).

Амортизируемые (project_cfs с amorts, 2026-07-02): принципал по графику
амортизаций MOEX, купоны от остаточного номинала (= сумма будущих амортизаций
после начала периода; поле face строк купонов ненадёжно). IG-сегмент n=16:
mean|Δ|≈113, медиана +81; хвост низких рейтингов — та же кросс-секция НРД.
"""
from __future__ import annotations
import logging
import math
from datetime import date
from typing import List, Optional

logger = logging.getLogger(__name__)

TENOR_Y = {"1W": 1/52, "2W": 2/52, "1M": 1/12, "2M": 2/12, "3M": .25, "6M": .5,
           "9M": .75, "1Y": 1, "2Y": 2, "3Y": 3, "4Y": 4, "5Y": 5, "6Y": 6,
           "7Y": 7, "8Y": 8, "9Y": 9, "10Y": 10}

# Лаг/конвенция фиксинга КС-купона per-issue — services.coupon_calib.period_index_pct
# (спека manual > калибратор из истории купонов; фолбэк — точечная КС на start).


class ExpCurve:
    """Кривая ожиданий индекса поверх честного bootstrap par-свопов (forwards.py).

    fwd(d1,d2) — эквивалентная ставка индекса на период в его собственной конвенции:
      RUONIA  — daily-comp average (project_cfs начисляет индекс (1+f/365)^days − 1 →
                factor DF воспроизводится точно; маржа — simple);
      KEYRATE — simple ACT/365 = уровень КС (начисление (КС+m)·days/365 → factor
                1+f·days/365 воспроизводится точно на периоде ЛЮБОЙ длины).
    spot(t) — уровень индекса на горизонте t лет той же конвенцией
    (короткий конец ≈ текущий фиксинг, заложенный в свопы)."""
    def __init__(self, calc_date: date, quotes, base: str = "RUONIA"):
        from datetime import timedelta
        from core.forwards import SheetForwardCurve
        self.base = base
        self.calc_date = calc_date
        self._td = timedelta
        # sheet-методика (вкладка КРИВЫЕ) — тот же источник, что прайсинг купонов
        self._curve = SheetForwardCurve(calc_date, quotes, base)
        self.rate_convention = self._curve.rate_convention

    def t(self, d: date) -> float:
        return (d - self.calc_date).days / 365.0

    def spot(self, t: float) -> float:
        d2 = self.calc_date + self._td(days=max(2, round(t * 365)))
        return self.fwd(self.calc_date, d2)

    def fwd(self, d1: date, d2: date) -> float:
        c = self._curve
        lo = max(d1, c.calc_date)          # кривая стартует с calc_date+1
        if d2 <= lo:
            d2 = lo + self._td(days=1)
        return c.forward(lo, d2)


class GCurve:
    """КБД ОФЗ: линейная интерполяция yearyields (%) по сроку τ (годы)."""
    def __init__(self, pts):  # [(period_years, yield_pct)]
        pts = sorted(pts)
        self.xs = [p[0] for p in pts]; self.ys = [p[1]/100.0 for p in pts]

    def ok(self) -> bool:
        return len(self.xs) >= 2

    def r(self, tau: float) -> float:
        xs, ys = self.xs, self.ys
        if not xs:
            return 0.0
        if tau <= xs[0]:
            return ys[0]
        if tau >= xs[-1]:
            return ys[-1]
        for i in range(1, len(xs)):
            if xs[i] >= tau:
                w = (tau-xs[i-1])/(xs[i]-xs[i-1])
                return ys[i-1]+w*(ys[i]-ys[i-1])
        return ys[-1]


def _d(s):
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def project_cfs(ref, exp: ExpCurve, calc_date: date, coupons: list, amorts: list = None,
                offers: list = None, index_pct_fn=None):
    """Прогноз потоков для z-спреда — ТОНКАЯ ОБЁРТКА над единым builder'ом
    valuation.build_cashflows_to_maturity (вход — MOEX-dicts, выход —
    [(pay_date, amount)]). Вся логика потоков (T+1/праздники, cut_at_offer,
    амортизации от остаточного номинала, достройка хвоста >100 купонов,
    факт-фиксинг начавшегося периода, конвенции RUONIA daily-comp /
    KEYRATE simple) живёт в ОДНОМ месте — раньше это была построчная копия
    (~150 строк), и каждый фикс вносился дважды (см. историю коммитов).

    Кривая: у ExpCurve берём внутреннюю bootstrap-кривую (та же конвенция
    forward, что в exp.fwd; builder клэмпит анкер к calc_date сам)."""
    from core.valuation import build_cashflows_to_maturity
    triples = []
    for c in coupons or []:
        e = _d(c.get("end"))
        if not e:
            continue
        s = _d(c.get("start")) or e
        triples.append((s, e, c.get("value")))
    curve = getattr(exp, "_curve", exp)
    cfs = build_cashflows_to_maturity(
        ref, curve, calc_date, explicit_periods=triples or None,
        amorts=amorts, offers=offers, index_pct_fn=index_pct_fn)
    return [(cf.pay_date, cf.amount_rub) for cf in cfs]


def solve_z_bps(g: GCurve, cfs, calc_date: date, dirty_target: float) -> Optional[int]:
    """z-спред (bps) над КБД: PV = Σ CF·exp(-(r_g(τ)+z)·τ) = dirty.
    τ — от ДАТЫ ПОСТАВКИ (T+1 раб), как и во всех метриках."""
    if not cfs or not g.ok():
        return None
    from core.valuation import settle_date
    anchor = settle_date(calc_date)

    def pv(z):
        tot = 0.0
        for pay, amt in cfs:
            tau = (pay - anchor).days / 365.0
            if tau <= 0:
                continue
            tot += amt * math.exp(-(g.r(tau) + z) * tau)
        return tot

    lo, hi = -0.5, 0.5
    flo, fhi = pv(lo) - dirty_target, pv(hi) - dirty_target
    if flo * fhi > 0:
        return None
    for _ in range(80):
        m = (lo + hi) / 2
        fm = pv(m) - dirty_target
        if abs(fm) < 1e-7:
            return round(m * 10000)
        if flo * fm < 0:
            hi = m
        else:
            lo, flo = m, fm
    return round((lo + hi) / 2 * 10000)


def solve_z_discrete(g: GCurve, cfs, calc_date: date, dirty_target: float) -> Optional[int]:
    """z-спред по методике НРД (met_rub 4.2 + Прил.2): дисконт по КБД ОФЗ МБ,
    ДИСКРЕТНЫЙ годовой компаундинг, ACT/365:

        P+A = Σ CF_i / (1 + G(τ_i) + Z)^(τ_i)

    где G(τ) — zero-ставка КБД Московской Биржи (G-curve). Отличие от старого
    solve_z_bps: дискрет `(1+G+Z)^(-τ)` вместо непрерывного `exp(-(G+Z)τ)`.
    Сверка RUONIA vs НРД z: median +15bp, mad 10 (n=25)."""
    if not cfs or not g.ok():
        return None
    from core.valuation import settle_date
    anchor = settle_date(calc_date)   # τ от даты поставки (T+1 раб)

    def pv(z: float) -> float:
        tot = 0.0
        for pay, amt in cfs:
            tau = (pay - anchor).days / 365.0
            if tau <= 0:
                continue
            base = 1.0 + g.r(tau) + z
            if base <= 0:
                return float("inf")
            tot += amt / base ** tau
        return tot

    lo, hi = -0.6, 0.9
    flo, fhi = pv(lo) - dirty_target, pv(hi) - dirty_target
    if flo != flo or fhi != fhi or flo * fhi > 0:
        # дистресс за пределами брекета / NaN — раньше None был неотличим от «нет данных»
        logger.warning(f"solve_z_discrete: нет корня в [{lo},{hi}] (pv(lo)-P={flo:.2f}, pv(hi)-P={fhi:.2f})")
        return None
    for _ in range(90):
        m = (lo + hi) / 2
        fm = pv(m) - dirty_target
        if abs(fm) < 1e-7:
            return round(m * 10000)
        if flo * fm < 0:
            hi = m
        else:
            lo, flo = m, fm
    return round((lo + hi) / 2 * 10000)


def solve_flat_y(cfs, calc_date: date, dirty_target: float) -> Optional[float]:
    """Плоская непрерывная доходность y: Σ CF·exp(−y·τ) = dirty."""
    if not cfs:
        return None
    from core.valuation import settle_date
    anchor = settle_date(calc_date)   # τ от даты поставки (T+1 раб)

    def pv(y):
        return sum(a * math.exp(-y * (p - anchor).days / 365.0)
                   for p, a in cfs if (p - anchor).days > 0)

    lo, hi = -0.5, 5.0
    if (pv(lo) - dirty_target) * (pv(hi) - dirty_target) > 0:
        return None
    for _ in range(100):
        m = (lo + hi) / 2
        if (pv(lo) - dirty_target) * (pv(m) - dirty_target) <= 0:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


def current_period_len(coupons: list, calc_date: date) -> float:
    """Длина текущего (или ближайшего будущего) купонного периода в годах —
    горизонт рефиксинга флоатера (конвенция duration НРД)."""
    best = None
    for c in coupons or []:
        s, e = _d(c.get("start")), _d(c.get("end"))
        if not s or not e:
            continue
        if s <= calc_date < e:
            return (e - s).days / 365.0
        if e > calc_date and (best is None or e < best[1]):
            best = (s, e)
    return (best[1] - best[0]).days / 365.0 if best else 0.25


def compute_z_bps(ref, exp: ExpCurve, g: GCurve, calc_date: date,
                  price_pct: float, accrued_rub: float, coupons: list,
                  amorts: list = None, offers: list = None,
                  need_dur: bool = True) -> tuple:
    """Высокоуровнево: (z-спред флоатера в bps, спред-дюрация в годах).

    need_dur=False — второй элемент всегда None, и лишний решатель не гоняется.
    ПРОДУ дюрация отсюда больше не нужна: её считает services.valuation._dur_block
    по потоку ВЫБРАННОГО горизонта (эта — всегда к погашению). Флаг оставлен
    для скриптов сверки, которые исторически читают пару.
    RUONIA — п.4.9 (z по кривой КБД); KEYRATE — (e^y − 1) − G(τ_reset), см. докстринг модуля.
    Для амортизируемых бумаг ref.face_value = остаточный номинал на calc_date
    (цена в % котируется от него), amorts — график погашения принципала."""
    if ref.base not in ("RUONIA", "KEYRATE") or price_pct is None:
        return None, None
    from core.valuation import face_for_pricing, settle_date, first_offer_date
    settle = settle_date(calc_date)
    # Погашение ≤ T+1: весь поток покупателю не достаётся, но residual-ветка
    # project_cfs всё равно добавила бы принципал (условие > calc_date) → z-мусор.
    # Тот же MATURED-guard, что в services.valuation.calculate_valuation_metrics.
    if ref.maturity_date is not None and ref.maturity_date <= settle:
        return None, None
    # Перп (нет maturity): поток не терминируется — residual-принципала не будет,
    # z решался бы против голых купонов до обрыва расписания (глубоко отрицательный
    # мусор, молча). Осмыслен только при оферте с обрезкой (cut_at_offer).
    if ref.maturity_date is None:
        put = None
        if offers:
            try:
                from services.ref_data import cut_at_offer
                if cut_at_offer(ref.isin):
                    put = first_offer_date(offers, settle)
            except Exception:
                put = None
        if put is None:
            return None, None
    dirty = face_for_pricing(ref.face_value, amorts, calc_date) * price_pct / 100.0 + (accrued_rub or 0.0)
    # I/O-граница: история индекса — один фетч здесь, в project_cfs — инжекция
    try:
        from functools import partial
        from services.coupon_calib import period_index_pct, index_history
        index_pct_fn = partial(period_index_pct, idx=index_history(ref.base))
    except Exception:
        index_pct_fn = lambda *a, **k: None   # деградация: начавшийся период → форвард
    cfs = project_cfs(ref, exp, calc_date, coupons, amorts, offers, index_pct_fn=index_pct_fn)
    # Macaulay тех же прогнозных потоков — спред-дюрация К ПОГАШЕНИЮ. Считаем
    # только по запросу: KEYRATE-ветке ниже плоская доходность нужна для самого
    # z, а RUONIA без need_dur обходится вовсе без этого решателя.
    dur = None
    y_for_dur = None
    if need_dur or ref.base == "KEYRATE":
        y_for_dur = solve_flat_y(cfs, calc_date, dirty)
    if need_dur and y_for_dur is not None:
        from services.metrics import macaulay_years
        dur = macaulay_years(cfs, calc_date, y_for_dur)
    if ref.base == "KEYRATE":
        if not g.ok():
            return None, dur
        if y_for_dur is None:
            return None, dur
        tau = current_period_len(coupons, calc_date)
        return round((math.exp(y_for_dur) - 1 - g.r(tau)) * 10000), dur
    # RUONIA — методика НРД: дисконт по КБД ОФЗ, дискретный годовой компаундинг
    return solve_z_discrete(g, cfs, calc_date, dirty), dur
