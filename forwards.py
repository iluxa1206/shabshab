import math
import calendar
from datetime import date, timedelta
from typing import List, Tuple, Callable

def yf_act365(d1: date, d2: date) -> float:
    """Измеряет Year Fraction (долю года) по конвенции ACT/365"""
    return (d2 - d1).days / 365.0


# ==================== Excel helper functions ====================
def excel_days_length(start_date: date, end_date: date, tenor: str) -> int:
    """Days length as in the Excel IRS sheet for RUONIA swaps.

    In the provided Excel, W/M tenors use actual calendar day difference (end-start),
    while all Y tenors use 365 (even for 2Y, 3Y, ...).
    """
    tenor = tenor.upper()
    if tenor.endswith('Y'):
        return 365
    return (end_date - start_date).days


def excel_implied_avg_rate(mid_rate_decimal: float, days_len: int) -> float:
    """Excel implied average rate (daily-comp equivalent) from mid quote and days length.

    Excel formula: ((1 + R * days/365)^(1/days) - 1) * 365
    where R is in decimal (e.g., 0.1512) and days is an integer.
    """
    if days_len <= 0:
        return 0.0
    accrual = 1.0 + mid_rate_decimal * (days_len / 365.0)
    if accrual <= 0:
        return 0.0
    return 365.0 * (math.pow(accrual, 1.0 / days_len) - 1.0)


def excel_keyrate_implied_avg_rate(mid_rate_decimal: float, tenor: str) -> float:
    """Implied average rate as on the IRS sheet for KEYRATE swaps."""
    tenor = tenor.upper()
    if tenor == "3M":
        return mid_rate_decimal
    if tenor == "6M":
        return 4.0 * (math.pow(1.0 + mid_rate_decimal / 2.0, 1.0 / 2.0) - 1.0)
    if tenor == "9M":
        return 4.0 * (math.pow(1.0 + mid_rate_decimal * 3.0 / 4.0, 1.0 / 3.0) - 1.0)
    return 4.0 * (math.pow(1.0 + mid_rate_decimal, 1.0 / 4.0) - 1.0)

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
    """Конвертация тенора вида 'ON', '1W', '3M', '1Y' в дату экспирации от start_date."""
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

def generate_quarterly_schedule(start_date: date, maturity_date: date) -> List[date]:
    """Генератор дат выплат для ежеквартальных купонов (KEYRATE IRS)."""
    dates = []
    current_date = start_date
    while current_date < maturity_date:
        current_date = add_months(current_date, 3)
        if current_date > maturity_date:
            break
        dates.append(current_date)
        
    if not dates or dates[-1] != maturity_date:
        dates.append(maturity_date)
    return dates

class DiscountCurve:
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
                
        # FLAT_FORWARD экстраполяция
        if len(self.nodes) < 2:
            return 1.0
            
        t_prev, df_prev = self.nodes[-2]
        t_last, df_last = self.nodes[-1]
        
        yf_seg = self.year_fraction(t_prev, t_last)
        f_last = (df_prev / df_last - 1.0) / yf_seg if yf_seg > 0 else 0.0
        
        yf_extrap = self.year_fraction(t_last, d)
        return df_last / (1.0 + f_last * yf_extrap)

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

    def forward_bps(self, t1: date, t2: date) -> int:
        return round(self.forward(t1, t2) * 10000)
        
    def forward_excel_style(self, anchor: date, end1: date, end2: date) -> float:
        """
        Форвард по логике Excel (лист IRS):
        - сначала считаем implied average rate до end как spot_daily_comp(end)
        - затем выделяем форвард между (end1, end2) через разницу накоплений

        Формула соответствует Excel-выражению вида:
            f = (r2*T2 - r1*T1) / ((end2-end1)/365)
        где Tn = (endn - anchor - 1) / 365, и сегментная доля года (end2-end1)/365.

        ВАЖНО: используется "-1 день" как в модели Excel.
        """
        if not (anchor < end1 < end2):
            raise ValueError("Require anchor < end1 < end2")

        # implied average rates (daily-comp equivalent) from DF
        r1 = self.spot_daily_comp_from_anchor(anchor, end1)
        r2 = self.spot_daily_comp_from_anchor(anchor, end2)

        # Excel IRS K-column forward is computed from implied average rates (r1, r2)
        # using year-fractions to the common anchor, BUT then normalized by the *segment* length.
        # In the sheet the logic is equivalent to:
        #   f = (r2*T2 - r1*T1) * 365 / (end2 - end1)
        # where Tn = (endn - anchor - 1)/365.

        t1 = (end1 - anchor).days - 1
        t2 = (end2 - anchor).days - 1
        if t1 <= 0 or t2 <= 0 or t2 <= t1:
            raise ValueError("Invalid day counts for Excel-style forward")

        T1 = t1 / 365.0
        T2 = t2 / 365.0

        seg_days = (end2 - end1).days
        if seg_days <= 0:
            return 0.0

        f = (r2 * T2 - r1 * T1) * 365.0 / seg_days
        return f

    def spot_daily_comp(self, d: date) -> float:
        """Spot-equivalent daily-comp из DF."""
        days = (d - self.calc_date).days
        if days <= 0:
            return 0.0
        
        df_val = self.df(d)
        if df_val <= 0:
            return 0.0
            
        return 365.0 * (math.pow(df_val, -1.0 / days) - 1.0)

    def spot_daily_comp_from_anchor(self, anchor: date, d: date) -> float:
        """Excel-style implied average rate from anchor to d using curve DFs."""
        if d <= anchor:
            return 0.0

        # df(anchor) may need interpolation if anchor is not a node
        df_anchor = self.df(anchor) if anchor > self.calc_date else 1.0
        df_d = self.df(d)
        if df_anchor <= 0 or df_d <= 0:
            return 0.0

        days = (d - anchor).days
        if days <= 0:
            return 0.0

        # Relative DF for (anchor -> d)
        df_rel = df_d / df_anchor
        return 365.0 * (math.pow(df_rel, -1.0 / days) - 1.0)
        
    def spot_daily_comp_bps(self, d: date) -> int:
        return round(self.spot_daily_comp(d) * 10000)


class ExcelForwardCurve(DiscountCurve):
    def __init__(self, calc_date: date, nodes: List[Tuple[date, float]], segments: List[Tuple[date, date, float]], base_type: str):
        super().__init__(calc_date, nodes)
        self.segments = segments
        self.base_type = base_type

    def _equivalent_rate(self, factor: float, days: int) -> float:
        if days <= 0:
            return 0.0
        if self.base_type == "RUONIA":
            return 365.0 * (math.pow(factor, 1.0 / days) - 1.0)

        alpha = days / 365.0
        if alpha <= 0:
            return 0.0
        return 4.0 * (math.pow(factor, 1.0 / (4.0 * alpha)) - 1.0)

    def _segment_factor(self, rate: float, days: int) -> float:
        if self.base_type == "RUONIA":
            return math.pow(1.0 + rate / 365.0, days)

        alpha = days / 365.0
        return math.pow(1.0 + rate / 4.0, 4.0 * alpha)

    def forward(self, t1: date, t2: date) -> float:
        if t1 >= t2:
            raise ValueError("t2 must be strictly > t1")

        total_days = (t2 - t1).days
        factor = 1.0
        covered_days = 0

        for seg_start, seg_end, seg_rate in self.segments:
            overlap_start = max(t1, seg_start)
            overlap_end = min(t2, seg_end)
            if overlap_end <= overlap_start:
                continue

            days = (overlap_end - overlap_start).days
            factor *= self._segment_factor(seg_rate, days)
            covered_days += days

        if covered_days == 0:
            seg_rate = self.segments[-1][2]
            return seg_rate

        if covered_days < total_days:
            tail_days = total_days - covered_days
            seg_rate = self.segments[-1][2]
            factor *= self._segment_factor(seg_rate, tail_days)

        return self._equivalent_rate(factor, total_days)


class CurveBootstrapper:
    @staticmethod
    def _build_excel_forward_curve(quotes: List, calc_date: date, base_type: str) -> ExcelForwardCurve:
        effective_start = calc_date + timedelta(days=1)
        sorted_quotes = sorted(quotes, key=lambda x: get_maturity_date(effective_start, x.tenor))

        nodes: List[Tuple[date, float]] = []
        segments: List[Tuple[date, date, float]] = []
        prev_end = None
        prev_implied = None

        for q in sorted_quotes:
            end_date = get_maturity_date(effective_start, q.tenor)
            if end_date <= effective_start:
                continue

            actual_days = (end_date - effective_start).days
            if actual_days <= 0:
                continue

            mid_rate_decimal = q.value / 100.0
            if base_type == "RUONIA":
                days_len = excel_days_length(effective_start, end_date, q.tenor)
                implied = excel_implied_avg_rate(mid_rate_decimal, days_len)
                df = 1.0 / math.pow(1.0 + implied / 365.0, actual_days)
            else:
                implied = excel_keyrate_implied_avg_rate(mid_rate_decimal, q.tenor)
                alpha = actual_days / 365.0
                df = 1.0 / math.pow(1.0 + implied / 4.0, 4.0 * alpha)

            if nodes and df > nodes[-1][1]:
                df = max(nodes[-1][1] * (1.0 - 1e-12), 1e-12)

            nodes.append((end_date, df))

            seg_start = effective_start if prev_end is None else prev_end
            if prev_end is None:
                forward = implied
            else:
                t_prev = (prev_end - effective_start).days / 365.0
                t_curr = (end_date - effective_start).days / 365.0
                seg_days = (end_date - prev_end).days + 1
                if seg_days <= 0:
                    continue
                forward = (implied * t_curr - prev_implied * t_prev) * 365.0 / seg_days

            segments.append((seg_start, end_date, forward))
            prev_end = end_date
            prev_implied = implied

        if not segments:
            raise ValueError(f"No segments built for {base_type} curve")

        return ExcelForwardCurve(effective_start, CurveBootstrapper._deduplicate_nodes(nodes), segments, base_type)

    @staticmethod
    def bootstrap_ruonia(quotes: List, calc_date: date) -> DiscountCurve:
        """
        Bootstrapping RUONIA OIS curve.
        Uses the same implied-rate and forward logic as the IRS Excel sheet.
        """
        return CurveBootstrapper._build_excel_forward_curve(quotes, calc_date, "RUONIA")

    @staticmethod
    def bootstrap_keyrate(quotes: List, calc_date: date) -> DiscountCurve:
        """
        Bootstrapping IRS KEYRATE curve.
        Uses the same implied-rate and forward logic as the IRS Excel sheet.
        """
        return CurveBootstrapper._build_excel_forward_curve(quotes, calc_date, "KEYRATE")

    @staticmethod
    def _bisection_solve(func: Callable[[float], float], a: float, b: float, tol: float = 1e-10, max_iter: int = 150) -> float:
        """Решение f(x)=0 на интервале [a, b]."""
        fa = func(a)
        fb = func(b)
        
        if abs(fa) < 1e-12: return a
        if abs(fb) < 1e-12: return b
        
        if fa * fb > 0:
            raise ValueError(f"No root found in interval [{a}, {b}]. f(a)={fa}, f(b)={fb}")
            
        for _ in range(max_iter):
            c = (a + b) / 2.0
            fc = func(c)
            
            if abs(fc) < tol or (b - a) / 2.0 < tol:
                return c
                
            if fa * fc < 0:
                b = c
                fb = fc
            else:
                a = c
                fa = fc
                
        return (a + b) / 2.0

    @staticmethod
    def _bracket_root(
        func: Callable[[float], float],
        a: float,
        b: float,
        shrink: float = 0.90,
        max_iter: int = 60,
    ) -> tuple[float, float]:
        """
        Находит интервал [a,b] где f(a)*f(b) <= 0.
        Если знака нет — сжимает b -> b*shrink.
        """
        fa = func(a)
        fb = func(b)

        if abs(fa) < 1e-12:
            return a, a
        if abs(fb) < 1e-12:
            return b, b
        if fa * fb <= 0:
            return a, b

        b0 = b
        for _ in range(max_iter):
            b = max(a * 1.000001, b * shrink)
            fb = func(b)
            if abs(fb) < 1e-12:
                return b, b
            if fa * fb <= 0:
                return a, b

        raise ValueError(
            f"Cannot bracket root in [{a}, {b0}]. f(a)={fa}, f(b0)={func(b0)}, last b={b}, f(b)={fb}"
        )

    @staticmethod
    def _deduplicate_nodes(nodes: List[Tuple[date, float]]) -> List[Tuple[date, float]]:
        unique = {}
        for d, df in nodes:
            if d in unique:
                # важно понимать, что при дубликате дата перезапишется
                # можно заменить на логгер, если используете logging
                print(f"[WARN] duplicate node date {d}, overwrite DF {unique[d]} -> {df}")
            unique[d] = df
        return sorted(unique.items(), key=lambda x: x[0])

# ========================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ (для локального тестирования)
# ========================================================================
if __name__ == "__main__":
    import os
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from rates import get_rates_curves, Quote

    ois_quotes, irs_quotes = get_rates_curves(use_cache=True)
    
    calc_date = date.today()
    if ois_quotes:
        calc_date = ois_quotes[0].date
        
    print(f"Calc date: {calc_date}")
    effective_calc_date = calc_date + timedelta(days=1)
    print(f"Effective start (Excel TODAY()+1): {effective_calc_date}")

    def print_forward_rates(curve_name: str, curve: DiscountCurve, quotes_list: List[Quote], base_date: date):
        """
        Печатает таблицу, максимально похожую на Excel (лист IRS):
        - Узлы по SPFI-tenors
        - Quote (mid)
        - DF
        - Implied average rate (эквивалент daily-comp) = spot_daily_comp
        - Forward rate между соседними узлами по формуле Excel ("-1 day")

        В Excel forward:
          - для первого узла forward = implied_avg
          - далее forward = (r2*T2 - r1*T1) * 365 / (end2-end1), где Tn=(endn-anchor-1)/365
        Здесь anchor = base_date (effective start), как в Excel (TODAY()+1).
        """
        if quotes_list:
            spfi_tenors = sorted(
                {q.tenor.upper() for q in quotes_list},
                key=lambda t: get_maturity_date(base_date, t),
            )
        else:
            spfi_tenors = ["1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y"]

        anchor_today = base_date - timedelta(days=1)
        print(f"\n--- {curve_name} Curve (Excel-style forwards) ---")
        print(f"Anchor (Excel TODAY): {anchor_today} | Start (Excel TODAY()+1): {base_date}")
        print(f"{'Tenor':<5} | {'End date':<10} | {'Days':>4} | {'DF':>10} | {'Quote':>9} | {'ImpliedAvg':>11} | {'Fwd(seg)':>14}")
        print("-" * 92)

        quote_map = {q.tenor.upper(): q.value for q in quotes_list}

        anchor = base_date
        prev_end = None
        prev_implied = None

        for tenor in spfi_tenors:
            end_date = get_maturity_date(base_date, tenor)
            days = (end_date - base_date).days

            try:
                df_val = curve.df(end_date)
                quote_val = quote_map.get(tenor)

                # Excel implied average rate is computed directly from the quote and the Excel 'days length'
                if quote_val is None:
                    implied = curve.spot_daily_comp_from_anchor(anchor, end_date)
                else:
                    days_len = excel_days_length(base_date, end_date, tenor)
                    implied = excel_implied_avg_rate(quote_val / 100.0, days_len)

                # Excel forward rate between neighboring nodes
                if prev_end is None:
                    fwd = implied
                else:
                    # Tn = (endn - anchor_today - 1)/365 as in the sheet
                    t1 = (prev_end - anchor_today).days - 1
                    t2 = (end_date - anchor_today).days - 1
                    if t1 <= 0 or t2 <= 0 or t2 <= t1:
                        raise ValueError("Invalid day counts for Excel forward")

                    T1 = t1 / 365.0
                    T2 = t2 / 365.0
                    seg_days = (end_date - prev_end).days
                    if seg_days <= 0:
                        fwd = 0.0
                    else:
                        # f = (I2*T2 - I1*T1) * 365 / (H2-H1)
                        fwd = (implied * T2 - prev_implied * T1) * 365.0 / seg_days

                df_str = f"{df_val:.8f}"
                quote_str = f"{quote_val:.4f}%" if quote_val is not None else "-"
                implied_str = f"{implied*100:.4f}%"
                fwd_str = f"{fwd*100:.4f}%"

                print(f"{tenor:<5} | {end_date} | {days:>4} | {df_str:>10} | {quote_str:>9} | {implied_str:>11} | {fwd_str:>14}")

                prev_end = end_date
                prev_implied = implied
            except Exception as e:
                print(f"[WARN] failed tenor={tenor} end_date={end_date}: {e}")

    # 1. RUONIA
    ruonia_curve = CurveBootstrapper.bootstrap_ruonia(ois_quotes, calc_date)
    print_forward_rates("RUONIA OIS", ruonia_curve, ois_quotes, effective_calc_date)
    
    # Smoke-test: Excel-style forward between last two nodes is stable
    try:
        anchor_today = effective_calc_date - timedelta(days=1)
        t4 = get_maturity_date(effective_calc_date, "4Y")
        t5 = get_maturity_date(effective_calc_date, "5Y")

        qmap = {q.tenor.upper(): q.value for q in ois_quotes}
        i4 = excel_implied_avg_rate(qmap["4Y"] / 100.0, excel_days_length(effective_calc_date, t4, "4Y"))
        i5 = excel_implied_avg_rate(qmap["5Y"] / 100.0, excel_days_length(effective_calc_date, t5, "5Y"))

        T4 = ((t4 - anchor_today).days - 1) / 365.0
        T5 = ((t5 - anchor_today).days - 1) / 365.0
        seg_days = (t5 - t4).days
        f_last_seg = (i5 * T5 - i4 * T4) * 365.0 / seg_days
        print(f"\n[SMOKE] Excel-style forward check: f(4Y->5Y)={f_last_seg*100:.4f}%")
    except Exception as e:
        print(f"\n[WARN] Excel-style forward smoke-test failed: {e}")
        
    # 2. KEYRATE IRS
    irs_curve = CurveBootstrapper.bootstrap_keyrate(irs_quotes, calc_date)
    print_forward_rates("KEYRATE IRS", irs_curve, irs_quotes, effective_calc_date)
