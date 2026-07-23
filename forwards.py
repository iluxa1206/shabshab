import math
import calendar
from datetime import date, timedelta
from typing import List, Tuple
import logging

logger = logging.getLogger(__name__)

def yf_act365(d1: date, d2: date) -> float:
    """Измеряет Year Fraction (долю года) по конвенции ACT/365"""
    return (d2 - d1).days / 365.0


def add_months(d: date, months: int) -> date:
    """Точное прибавление календарных месяцев без сдвигов по выходным (CalendarMode = NONE)."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = d.day
    
    try:
        return date(year, month, day)
    except ValueError:
        _, last_day = calendar.monthrange(year, month)
        return date(year, month, min(day, last_day))

def get_maturity_date(start_date: date, tenor: str) -> date:
    """Конвертация тенора вида 'ON', '1W', '3M', '1Y' в дату экспирации от start_date.

    ВНИМАНИЕ (конвенция): все теноры получают +1 день (1Y = 366д от effective
    start) — эмпирическая подгонка под подневную сетку СПФИ-кривой из
    502_504.xlsm (реплика листа сверялась с Excel по дням). Фикс-нога при этом
    строится add_months БЕЗ +1 (см. _fixed_leg_schedule) — последний период
    короче на день; эффект — единицы bps в DF дальних узлов. При переходе на
    формальную методику СПФИ (business-day adjusted даты) пересмотреть оба места
    согласованно."""
    tenor = tenor.upper()
    if tenor == "ON": return start_date + timedelta(days=1)
    if tenor == "TN": return start_date + timedelta(days=2)
    if tenor == "SN": return start_date + timedelta(days=3)
    
    if tenor.endswith('D'):
        return start_date + timedelta(days=int(float(tenor[:-1]))) + timedelta(days=1)
    if tenor.endswith('W'):
        return start_date + timedelta(days=int(float(tenor[:-1]) * 7)) + timedelta(days=1)
    if tenor.endswith('M'):
        return add_months(start_date, int(float(tenor[:-1]))) + timedelta(days=1)
    if tenor.endswith('Y'):
        return add_months(start_date, int(float(tenor[:-1]) * 12)) + timedelta(days=1)
    
    raise ValueError(f"Unknown tenor format: {tenor}")

class DiscountCurve:
    # Конвенция ставки, которую возвращает forward():
    #   simple     — простая ACT/365: (DF1/DF2 − 1)·365/days
    #   daily_comp — эквивалент дневной капитализации: 365·((DF1/DF2)^(1/days) − 1)
    #   level      — сырой уровень индекса (плоские кривые)
    # Потребители (pv_cashflows_with_dm, build_cashflows) начисляют купон обратно
    # конвенцией БАЗЫ бумаги — несоответствие кривая↔база даёт двойной компаундинг
    # и раньше никак не проверялось (неявный контракт по типу кривой).
    rate_convention: str = "simple"

    def __init__(self, calc_date: date, nodes: List[Tuple[date, float]] = None):
        """nodes: список кортежей (date, df). calc_date неявно добавляется с DF=1.0"""
        self.calc_date = calc_date
        self.nodes = [(calc_date, 1.0)]
        
        if nodes:
            for d, df in sorted(nodes, key=lambda x: x[0]):
                if d > self.nodes[-1][0]:
                    self.nodes.append((d, df))
                    
        self._validate_curve()
                    
    def _validate_curve(self):
        """Проверки корректности (assertions)."""
        for i in range(len(self.nodes)):
            d, df = self.nodes[i]
            if not (0.0 < df <= 1.0):
                raise ValueError(f"DF must be in (0, 1]. Found {df} at {d}")
            if i > 0:
                prev_df = self.nodes[i-1][1]
                if df > prev_df:
                    raise ValueError(f"DF must be strictly non-increasing. {prev_df} -> {df} at {d}")

    def year_fraction(self, d1: date, d2: date) -> float:
        return yf_act365(d1, d2)

    def df(self, d: date) -> float:
        """Интерполяция дисконт-фактора."""
        if d <= self.calc_date:
            return 1.0
            
        for i in range(1, len(self.nodes)):
            if self.nodes[i][0] == d:
                return self.nodes[i][1]
            if self.nodes[i][0] > d:
                d1, df1 = self.nodes[i-1]
                d2, df2 = self.nodes[i]
                
                time_ratio = self.year_fraction(d1, d) / self.year_fraction(d1, d2)
                ln_df1 = math.log(df1)
                ln_df2 = math.log(df2)
                
                ln_df_target = ln_df1 + time_ratio * (ln_df2 - ln_df1)
                return math.exp(ln_df_target)
                
        # FLAT_FORWARD экстраполяция: продолжаем НАКЛОН ln(DF) последнего сегмента
        # (лог-линейно) — непрерывный форвард за последним узлом константен, и
        # эквивалентная ставка сегмента (daily-comp/квартальная) не убывает со
        # сроком. Старый вариант df_last/(1+f·τ) держал ПРОСТУЮ ставку от узла →
        # форвард дальних сегментов гиперболически затухал (14% → ~8% на +5Y),
        # занижая дальние купоны бумаг длиннее последнего свопа.
        if len(self.nodes) < 2:
            return 1.0

        t_prev, df_prev = self.nodes[-2]
        t_last, df_last = self.nodes[-1]

        yf_seg = self.year_fraction(t_prev, t_last)
        if yf_seg <= 0:
            return df_last

        yf_extrap = self.year_fraction(t_last, d)
        ln_slope = (math.log(df_last) - math.log(df_prev)) / yf_seg
        return df_last * math.exp(ln_slope * yf_extrap)

    def forward(self, t1: date, t2: date) -> float:
        """Расчет простого форварда F = (DF(t1)/DF(t2) - 1) / YF(t1,t2)."""
        if t1 >= t2:
            raise ValueError("t2 must be strictly > t1")
            
        df1 = self.df(t1)
        df2 = self.df(t2)
        
        yf = self.year_fraction(t1, t2)
        if yf == 0:
            return 0.0
            
        return (df1 / df2 - 1.0) / yf

class BootstrappedForwardCurve(DiscountCurve):
    """Кривая из честного bootstrap par-свопов (NPV=0 на каждом теноре).

    DF интерполируется log-linear (унаследовано от DiscountCurve),
    forward(t1,t2) возвращает эквивалентную ставку сегмента в конвенции индекса:
      RUONIA  — daily-comp average: 365·(factor^(1/days) − 1)
      KEYRATE — simple ACT/365:     (factor − 1)·365/days
    где factor = DF(t1)/DF(t2). Потребители (cashflow, valuation) начисляют
    купон обратно теми же конвенциями — factor воспроизводится точно на периоде
    любой длины (см. _equivalent_rate о том, почему KEYRATE именно простая).
    """
    def __init__(self, calc_date: date, nodes: List[Tuple[date, float]], base_type: str):
        super().__init__(calc_date, nodes)
        self.base_type = base_type
        self.rate_convention = "daily_comp" if base_type == "RUONIA" else "simple"

    def _equivalent_rate(self, factor: float, days: int) -> float:
        """factor = DF(t1)/DF(t2) → ставка сегмента в конвенции индекса.

        Обе ветки подобраны так, чтобы потребитель, начисляя купон обратно своей
        конвенцией, воспроизвёл factor ТОЧНО на периоде ЛЮБОЙ длины:
          RUONIA  — daily-comp: (1+f/365)^days == factor;
          KEYRATE — simple:      1 + f·days/365 == factor.

        KEYRATE-ветка раньше возвращала quarterly-nominal 4·(factor^(1/4α)−1).
        Это тождественно простой ставке ТОЛЬКО при α=0.25 — на прочих периодах
        она подмешивала несуществующую выпуклость: замер при КС 16% давал
        +21.0 bps/год на месячном купоне и −31.8 bps/год на полугодовом
        (квартальный — +0.1 bps, т.е. ошибки не было ровно там, где её искали).
        Простая ставка здесь не приближение, а точная инверсия: bootstrap решает
        par-своп из S·Στ_i·DF_i = 1 − DF_n, а это равенство верно тогда и только
        тогда, когда float-нога накапливается простым форвардом, Π(1+f_i·τ_i) =
        DF(t1)/DF(t2). Конвенция выпусков ((КС+маржа)·days/365 simple ACT/365)
        совпадает с ней же — поэтому DF-цепочка в pv_cashflows_with_dm при dm=0
        теперь в точности равна DF кривой.
        """
        if days <= 0 or factor <= 0:
            return 0.0
        if self.base_type == "RUONIA":
            return 365.0 * (math.pow(factor, 1.0 / days) - 1.0)
        # KEYRATE simple ACT/365 точен только на КОРОТКОМ сегменте (купон/квартал):
        # на длинном одиночном пролёте (1+f·days/365)=factor раздувает f (10Y ~35%).
        # Все текущие потребители зовут forward() короткими смежными сегментами
        # (телескопирование). Одиночный длинный вызов — ошибка использования.
        if days > 400:
            logger.warning(
                "KEYRATE forward() на длинном пролёте %d дн — simple-ставка раздувается; "
                "используйте короткие сегменты или df()-спот (spot_pct в /curves/plot)", days)
        return (factor - 1.0) * 365.0 / days

    def forward(self, t1: date, t2: date) -> float:
        if t1 >= t2:
            raise ValueError("t2 must be strictly > t1")
        factor = self.df(t1) / self.df(t2)
        return self._equivalent_rate(factor, (t2 - t1).days)


class CurveBootstrapper:
    @staticmethod
    def _fixed_leg_schedule(start: date, end: date, months: int) -> List[date]:
        """Даты платежей фикс-ноги каждые `months` месяцев; последняя = end."""
        out: List[date] = []
        i = 1
        while True:
            d = add_months(start, months * i)
            if d >= end:
                break
            out.append(d)
            i += 1
        out.append(end)
        return out

    @staticmethod
    def _bootstrap_par_curve(quotes: List, calc_date: date, base_type: str) -> BootstrappedForwardCurve:
        """Честный bootstrap: для каждого тенора решаем DF(end) из условия
        par-свопа S·Σ τ_i·DF_i = 1 − DF_n (single-curve: проекция = дисконт).

        Фикс-нога: RUONIA — разовый платёж для теноров ≤1Y, годовая для длинных;
        KEYRATE — квартальная (конвенция СПФИ МБ). Промежуточные DF при решении —
        log-linear между известными узлами и кандидатом (бисекция, монотонный NPV).
        """
        effective_start = calc_date + timedelta(days=1)
        sorted_quotes = sorted(quotes, key=lambda x: get_maturity_date(effective_start, x.tenor))
        nodes: List[Tuple[date, float]] = []

        def df_interp(d: date, trial: Tuple[date, float]) -> float:
            pts = sorted([(effective_start, 1.0)] + nodes + [trial])
            if d <= effective_start:
                return 1.0
            for i in range(1, len(pts)):
                if pts[i][0] == d:
                    return pts[i][1]
                if pts[i][0] > d:
                    (d1, f1), (d2, f2) = pts[i - 1], pts[i]
                    w = yf_act365(d1, d) / yf_act365(d1, d2)
                    return math.exp(math.log(f1) + w * (math.log(f2) - math.log(f1)))
            # flat-forward хвост (нужен только для дат за кандидатом — не встречается)
            (dp, fp), (dl, fl) = pts[-2], pts[-1]
            slope = (math.log(fl) - math.log(fp)) / yf_act365(dp, dl)
            return fl * math.exp(slope * yf_act365(dl, d))

        for q in sorted_quotes:
            end_date = get_maturity_date(effective_start, q.tenor)
            if end_date <= effective_start:
                continue
            S = q.value / 100.0
            days = (end_date - effective_start).days

            if base_type == "RUONIA" and days <= 366:
                df = 1.0 / (1.0 + S * yf_act365(effective_start, end_date))
            else:
                months = 12 if base_type == "RUONIA" else 3
                sched = CurveBootstrapper._fixed_leg_schedule(effective_start, end_date, months)

                def npv(df_end: float) -> float:
                    trial = (end_date, df_end)
                    pv_fix, prev = 0.0, effective_start
                    for d in sched:
                        pv_fix += yf_act365(prev, d) * df_interp(d, trial)
                        prev = d
                    return S * pv_fix - (1.0 - df_end)  # fix − float; растёт по df_end

                lo, hi = 1e-6, 1.0
                for _ in range(80):
                    mid = (lo + hi) / 2.0
                    if npv(mid) >= 0.0:
                        hi = mid
                    else:
                        lo = mid
                df = (lo + hi) / 2.0

            if nodes and df > nodes[-1][1]:
                # DF растёт (форвард < 0) — не-арбитражный клэмп до ~плоского.
                # Раньше молча: на инвертированном коротком конце (цикл снижения)
                # это зануляло реальный форвард сегмента. Логируем — видно в prod.
                logger.warning(
                    "%s curve: DF инверсия на теноре %s (DF %.6f > пред %.6f) — "
                    "форвард сегмента зажат в ~0%%, проекция купона на нём занижена",
                    base_type, end_date.isoformat(), df, nodes[-1][1])
                df = max(nodes[-1][1] * (1.0 - 1e-12), 1e-12)
            nodes.append((end_date, df))

        if not nodes:
            raise ValueError(f"No nodes built for {base_type} curve")
        return BootstrappedForwardCurve(effective_start, CurveBootstrapper._deduplicate_nodes(nodes), base_type)

    @staticmethod
    def bootstrap_ruonia(quotes: List, calc_date: date) -> DiscountCurve:
        """RUONIA OIS: честный bootstrap par-квот (фикс-нога single ≤1Y / годовая)."""
        return CurveBootstrapper._bootstrap_par_curve(quotes, calc_date, "RUONIA")

    @staticmethod
    def bootstrap_keyrate(quotes: List, calc_date: date) -> DiscountCurve:
        """IRS KEYRATE: честный bootstrap par-квот (фикс-нога квартальная)."""
        return CurveBootstrapper._bootstrap_par_curve(quotes, calc_date, "KEYRATE")

    @staticmethod
    def _deduplicate_nodes(nodes: List[Tuple[date, float]]) -> List[Tuple[date, float]]:
        unique = {}
        for d, df in nodes:
            if d in unique:
                # важно понимать, что при дубликате дата перезапишется
                # можно заменить на логгер, если используете logging
                logger.warning(f"[WARN] duplicate node date {d}, overwrite DF {unique[d]} -> {df}")
            unique[d] = df
        return sorted(unique.items(), key=lambda x: x[0])

# ========================================================================
# ЛОКАЛЬНАЯ ПРОВЕРКА: дамп bootstrap-кривых по узлам СПФИ
# ========================================================================
if __name__ == "__main__":
    import os
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from rates import get_rates_curves

    ois_quotes, irs_quotes = get_rates_curves(use_cache=True)
    calc_date = ois_quotes[0].date if ois_quotes else date.today()
    start = calc_date + timedelta(days=1)
    print(f"Calc date: {calc_date} | Effective start: {start}")

    def dump(name: str, curve: DiscountCurve, quotes: List) -> None:
        """Узел за узлом: DF, par-квота и форвард сегмента в конвенции индекса.

        Форвард печатаем МАРЖИНАЛЬНЫЙ (между соседними узлами) — именно он идёт
        в проекцию купонов. За последним узлом работает flat-forward
        экстраполяция DiscountCurve.df (наклон последнего сегмента продолжается).
        """
        tenors = sorted({q.tenor.upper() for q in quotes},
                        key=lambda t: get_maturity_date(start, t))
        qmap = {q.tenor.upper(): q.value for q in quotes}
        print(f"\n--- {name} ---")
        print(f"{'Tenor':<5} | {'End date':<10} | {'Days':>5} | {'DF':>12} | {'Par':>8} | {'Fwd(seg)':>9}")
        print("-" * 68)
        prev = start
        for t in tenors:
            end = get_maturity_date(start, t)
            try:
                fwd = curve.forward(prev, end) if prev < end else 0.0
                print(f"{t:<5} | {end} | {(end-start).days:>5} | {curve.df(end):>12.8f} | "
                      f"{qmap[t]:>7.4f}% | {fwd*100:>8.4f}%")
                prev = end
            except Exception as e:
                print(f"[WARN] {t} @ {end}: {e}")

    dump("RUONIA OIS", CurveBootstrapper.bootstrap_ruonia(ois_quotes, calc_date), ois_quotes)
    dump("KEYRATE IRS", CurveBootstrapper.bootstrap_keyrate(irs_quotes, calc_date), irs_quotes)

    # Экстраполяция за последним свопом: форвард обязан остаться константой
    ks = CurveBootstrapper.bootstrap_keyrate(irs_quotes, calc_date)
    last = ks.nodes[-1][0]
    print(f"\n[SMOKE] flat-forward за последним узлом ({last}):")
    for y in (11, 13, 15, 20):
        a = start + timedelta(days=365 * y)
        print(f"  {y:>2}y-{y+1:>2}y: {ks.forward(a, a + timedelta(days=365))*100:.4f}%")
