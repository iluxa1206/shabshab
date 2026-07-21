"""Траектории ожиданий КС из котировок СПФИ IRS KeyRate.

Два независимых объекта, оба ТОЛЬКО для отображения на графике КС дашборда
(services.ks_path.build_path). Прайсинг (SM/DM/z) НИ ОДИН из них не использует —
он дисконтирует по bootstrap-кривой (forwards.CurveBootstrapper).

1) excel_ks_forward_segments — реплика листа IRS файла 502_504 (маржинальный
   форвард между тенорами). Ступени до 10Y, без экстраполяции.

2) KsExpectationCurve — методика НРД met_float Прил.3: ОЖИДАЕМАЯ КС(t).
   • внутри [T_1, T_N] — натуральный кубический сплайн по ставкам СПФИ (кривая
     проходит через опорные par-ставки свопов);
   • за последним свопом T_N — экспо-затухание к долгосрочной нейтрали ЦБ ω:
       КС(t) = ω + (КС(T_N) − ω)·exp(−β·(t − T_N)).
   ВАЖНО — DISPLAY-ONLY, НЕ ДЛЯ ПРАЙСИНГА. Кривая кладёт par-ставку свопа как
   уровень КС на срок t (методика НРД так и определяет «ожидаемую КС»); это НЕ
   арбитраж-консистентно с дисконт-кривой (par ≠ мгновенный форвард), поэтому
   как источник дисконта её использовать нельзя — ровно за это она была убрана
   из прайсинга 2026-07-20. Как отдельная линия ОЖИДАНИЯ (взгляд «куда пойдёт
   КС» с реверсией к нейтрали, в отличие от арбитражного форварда) — легитимна.
"""
from __future__ import annotations
import math
import calendar as _cal
from datetime import date as _date
from typing import List, Tuple

# Долгосрочная нейтральная КС (прогноз ЦБ, середина диапазона 7.5–8.5%)
KS_NEUTRAL_PCT = 8.0
# Скорость затухания к нейтрали за последним свопом (1/год); полупериод ln2/β ≈ 3г
KS_DECAY_BETA = 0.23
# срочности теноров в годах (для сплайна ожидаемой КС)
_TENOR_Y = {"3M": .25, "6M": .5, "9M": .75, "1Y": 1, "2Y": 2, "3Y": 3, "4Y": 4,
            "5Y": 5, "6Y": 6, "7Y": 7, "8Y": 8, "9Y": 9, "10Y": 10}


def _natural_cubic_spline(xs: List[float], ys: List[float]):
    n = len(xs)
    if n < 2:
        y0 = ys[0] if ys else 0.0
        return lambda x: y0
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    a = [0.0] * n
    for i in range(1, n - 1):
        a[i] = 3 * ((ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1])
    l = [1.0] + [0.0] * (n - 1)
    mu = [0.0] * n
    z = [0.0] * n
    for i in range(1, n - 1):
        l[i] = 2 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        mu[i] = h[i] / l[i]
        z[i] = (a[i] - h[i - 1] * z[i - 1]) / l[i]
    c = [0.0] * n
    b = [0.0] * n
    d = [0.0] * n
    for j in range(n - 2, -1, -1):
        c[j] = z[j] - mu[j] * c[j + 1]
        b[j] = (ys[j + 1] - ys[j]) / h[j] - h[j] * (c[j + 1] + 2 * c[j]) / 3
        d[j] = (c[j + 1] - c[j]) / (3 * h[j])

    def ev(x):
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = 0
        while i < n - 1 and xs[i + 1] < x:
            i += 1
        dx = x - xs[i]
        return ys[i] + b[i] * dx + c[i] * dx * dx + d[i] * dx ** 3
    return ev


class KsExpectationCurve:
    """Ожидаемая КС(t) по НРД met_float Прил.3: сплайн par-свопов + экспо-затухание
    к нейтрали. DISPLAY-ONLY (см. докстринг модуля — НЕ для прайсинга).
    neutral_pct=None → долгосрочная нейтраль из прогноза ЦБ (cbr_forecast)."""
    def __init__(self, quotes, neutral_pct: float = None, beta: float = KS_DECAY_BETA):
        if neutral_pct is None:
            try:
                from services.cbr_forecast import neutral_pct as _fc_neutral
                neutral_pct = _fc_neutral(KS_NEUTRAL_PCT)
            except Exception:
                neutral_pct = KS_NEUTRAL_PCT
        pts = sorted((_TENOR_Y[q.tenor.upper()], q.value / 100.0)
                     for q in quotes if q.tenor.upper() in _TENOR_Y)
        self.xs = [p[0] for p in pts]
        self.ys = [p[1] for p in pts]
        self._spline = _natural_cubic_spline(self.xs, self.ys)
        self.t_last = self.xs[-1] if self.xs else 0.0
        self.ks_last = self.ys[-1] if self.ys else neutral_pct / 100.0
        self.neutral = neutral_pct / 100.0
        self.beta = beta

    def ks(self, t: float) -> float:
        """Ожидаемая КС (decimal) на срок t лет от даты расчёта."""
        if not self.xs:
            return self.neutral
        if t <= self.t_last:
            return self._spline(t)
        return self.neutral + (self.ks_last - self.neutral) * math.exp(-self.beta * (t - self.t_last))


# ── Точная реплика траектории КС из СПФИ (лист IRS файла 502_504) ──────────
_TENOR_MONTHS = {"3M": 3, "6M": 6, "9M": 9, "1Y": 12, "2Y": 24, "3Y": 36,
                 "4Y": 48, "5Y": 60, "6Y": 72, "7Y": 84, "8Y": 96, "9Y": 108,
                 "10Y": 120}


def _eomonth(d: _date, m: int) -> _date:
    """EOMONTH(d, m) — последний день месяца (d.month + m)."""
    month = d.month - 1 + m
    y = d.year + month // 12
    mo = month % 12 + 1
    return _date(y, mo, _cal.monthrange(y, mo)[1])


def _end_date(F: _date, months: int) -> _date:
    """H = EOMONTH(F, months−1) + DAY(F) (как в файле)."""
    return _eomonth(F, months - 1) + (_date(F.year, F.month, F.day) - _date(F.year, F.month, 1))


def _ks_implied_avg(mid: float, tenor: str) -> float:
    """Implied average rate КС из mid свопа (конвенция файла, лист IRS кол.I)."""
    t = tenor.upper()
    if t == "3M":
        return mid
    if t == "6M":
        return 4.0 * ((1 + mid / 2) ** (1 / 2) - 1)
    if t == "9M":
        return 4.0 * ((1 + mid * 3 / 4) ** (1 / 3) - 1)
    return 4.0 * ((1 + mid) ** (1 / 4) - 1)


def excel_ks_forward_segments(quotes, calc_date: _date) -> List[Tuple[_date, _date, float]]:
    """Траектория КС по логике файла (лист IRS, forward rate кол.K): маржинальный
    форвард между тенорами, даты по EOMONTH. [(start, end, forward_decimal)] — ступени.

    K_n = (I_n·(H_n−F) − I_{n-1}·(H_{n-1}−F)) / (H_n − H_{n-1}),
    анкер F = start (TODAY()+1) для ВСЕХ сегментов: implied avg I измерены от F,
    только с общим анкером маржинальный форвард телескопируется.

    В самом файле анкер корректен лишь в первой форвард-строке (K5 якорит H3 =
    TODAY); дальше формула протянута и анкер съехал на H_{n-2} (drag-баг:
    K6 = ...H4..., K7 = ...H5...). Ошибка ступени = (H_{n-2}−F)·(I_n−I_{n-1})/seg —
    до десятков bps на дальних тенорах. Здесь воспроизводим ЗАМЫСЕЛ формулы
    (фиксированный анкер), а не её drag-артефакт.
    Старт сегмента n = H_{n-1}, конец = H_n. Первый сегмент = spot implied.
    """
    F = calc_date + (_date(1, 1, 2) - _date(1, 1, 1))  # TODAY()+1
    qmap = {}
    for q in quotes:
        t = q.tenor.upper()
        if t in _TENOR_MONTHS:
            qmap[t] = q.value / 100.0
    order = [t for t in ["3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y",
                         "6Y", "7Y", "8Y", "9Y", "10Y"] if t in qmap]
    if not order:
        return []
    # узлы: [Срок(H=F), 3m, 6m, ...]; H — end date, I — implied avg
    H = [F]
    Iv = [None]  # у Срок implied не нужен для форвардов
    for t in order:
        H.append(_end_date(F, _TENOR_MONTHS[t]))
        Iv.append(_ks_implied_avg(qmap[t], t))

    segs: List[Tuple[_date, _date, float]] = []
    for k in range(1, len(order) + 1):
        start = H[k - 1]     # J = H_{n-1}
        end = H[k]
        if k == 1:
            fwd = Iv[1]       # spot implied
        else:
            # общий анкер F: (H−(F−1))−1 = H−F дней — как в K5 файла (H3=TODAY=F−1)
            dn = (end - F).days
            dp = (H[k - 1] - F).days
            seg = (end - H[k - 1]).days
            fwd = (Iv[k] * dn - Iv[k - 1] * dp) / seg if seg else Iv[k]
        segs.append((start, end, fwd))
    return segs
