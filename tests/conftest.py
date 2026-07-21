"""Общие фикстуры golden-тестов ядра.

Всё детерминировано и БЕЗ сети: кривые строятся из синтетических par-квот,
история индекса ЦБ инжектируется явными парами, провайдер индекса — чистая
замена без I/O. Так тесты воспроизводимы и страхуют рефакторинг ядра
(build_cashflows/солверы/bootstrap), где раньше был скрытый фетч cbr.ru.
"""
import os
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rates import Quote  # noqa: E402
from forwards import CurveBootstrapper, DiscountCurve  # noqa: E402
from valuation import BondRefData  # noqa: E402


CALC_DATE = date(2026, 1, 12)  # понедельник, вне праздников MOEX


def _flat_quotes(level_pct: float, calc_date: date):
    """Плоские par-квоты СПФИ на всех тенорах = level_pct. Плоская par-кривая →
    плоский форвард ≈ level (с точностью до конвенции фикс-ноги)."""
    tenors = ["1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y"]
    return [Quote("SYN", t, level_pct, calc_date) for t in tenors]


@pytest.fixture
def calc_date():
    return CALC_DATE


@pytest.fixture
def keyrate_curve():
    """KEYRATE IRS bootstrap на плоских 15% (rate_convention='simple')."""
    return CurveBootstrapper.bootstrap_keyrate(_flat_quotes(15.0, CALC_DATE), CALC_DATE)


@pytest.fixture
def ruonia_curve():
    """RUONIA OIS bootstrap на плоских 15% (rate_convention='daily_comp')."""
    return CurveBootstrapper.bootstrap_ruonia(_flat_quotes(15.0, CALC_DATE), CALC_DATE)


@pytest.fixture
def flat_index_15():
    """Инжектируемая история индекса: ровно 15% каждый день за 3 года до calc.
    Возвращает провайдер period_index_pct с забинженной историей idx."""
    from functools import partial
    from services.coupon_calib import period_index_pct
    dates, rates = [], []
    d = CALC_DATE - timedelta(days=365 * 3)
    while d <= CALC_DATE:
        dates.append(d)
        rates.append(15.0)  # история хранит проценты
        d += timedelta(days=1)
    idx = (dates, rates)
    return partial(period_index_pct, idx=idx), idx


def make_bond(base="KEYRATE", margin_bps=150, face=1000.0, accrued=0.0,
              maturity=date(2030, 1, 12), issue=date(2024, 1, 12),
              coupons_per_year=4, isin="RU_TEST_0001"):
    """Синтетический флоатер. По умолчанию — 4-летний квартальный KEYRATE+150."""
    first = date(issue.year, issue.month, issue.day) + timedelta(days=91)
    return BondRefData(
        isin=isin, base=base, spread_issue_bps=margin_bps, face_value=face,
        accrued_rub=accrued, maturity_date=maturity, first_coupon_date=first,
        coupons_per_year=coupons_per_year, issue_date=issue,
        coupon_period_days=round(365 / coupons_per_year),
    )


def quarterly_periods(issue: date, maturity: date, step_days=91):
    """Тройки (start_iso, end_iso, value=None) — синтетическое расписание купонов
    без зафиксированных сумм (всё проецируется форвардом/индексом)."""
    out = []
    s = issue
    while s < maturity:
        e = min(s + timedelta(days=step_days), maturity)
        out.append((s.isoformat(), e.isoformat(), None))
        s = e
    return out
