"""PV дисконтирует СУММУ платежей на дату и не зависит от длины потока квадратично.

Раньше на каждую дату сетки PV заново перебирал весь поток
(«sum(... for cf in cashflows if cf.pay_date == d)»), то есть был квадратичным
по числу купонов. У обычной бумаги (20–40 платежей) это незаметно, а у
тридцатилетнего ипотечного агента с ежемесячным купоном (ИАДОМ 1P62, 368
платежей) один PV стоил 135 тысяч сравнений — и солвер, зовущий PV полтора
десятка раз на каждую цену стакана, держал ядро 700 мс на одну бумагу.
"""
import time
from datetime import date, timedelta

from core.valuation import BondRefData, Cashflow, pv_cashflows_with_dm

CALC = date(2026, 9, 1)


class _Flat:
    """Плоская форвардная кривая: тест про суммирование потока, не про кривую."""
    rate_convention = "simple"

    def __init__(self, r):
        self.r = r

    def forward(self, t1, t2):
        return self.r


def _bond():
    return BondRefData(isin="TEST", base="KEYRATE", spread_issue_bps=200,
                       face_value=1000.0, accrued_rub=0.0,
                       maturity_date=date(2056, 12, 28),
                       first_coupon_date=date(2026, 10, 1),
                       coupons_per_year=12, coupon_period_days=30)


def _flows(n, coupon=14.0):
    out, d = [], CALC + timedelta(days=30)
    for _ in range(n):
        out.append(Cashflow(pay_date=d, amount_rub=coupon, type="COUPON"))
        d += timedelta(days=30)
    out.append(Cashflow(pay_date=d, amount_rub=1000.0, type="REDEMPTION"))
    return out


def test_several_cashflows_on_one_date_are_summed():
    """Купон, амортизация и погашение могут прийти в один день — PV обязан
    дисконтировать их сумму, а не первый попавшийся."""
    curve = _Flat(0.17)
    d = CALC + timedelta(days=30)
    one = [Cashflow(pay_date=d, amount_rub=1014.0, type="REDEMPTION")]
    split = [Cashflow(pay_date=d, amount_rub=14.0, type="COUPON"),
             Cashflow(pay_date=d, amount_rub=1000.0, type="REDEMPTION")]
    assert (pv_cashflows_with_dm(_bond(), curve, one, CALC, 250)
            == pv_cashflows_with_dm(_bond(), curve, split, CALC, 250))


def test_past_cashflows_are_ignored():
    """Платежи до даты поставки в PV не входят — их уже получил прошлый
    держатель."""
    curve = _Flat(0.17)
    live = _flows(4)
    past = [Cashflow(pay_date=CALC - timedelta(days=10), amount_rub=99.0,
                     type="COUPON")] + live
    assert (pv_cashflows_with_dm(_bond(), curve, past, CALC, 250)
            == pv_cashflows_with_dm(_bond(), curve, live, CALC, 250))


def test_long_bond_is_not_quadratic():
    """Длинный поток считается ЛИНЕЙНО.

    Порог с большим запасом — тест про порядок роста, а не про абсолютную
    скорость машины: при квадратичном PV поток из 368 платежей был в сотню раз
    дороже потока из 36, теперь разница около десятка."""
    curve = _Flat(0.17)
    bond = _bond()

    def ms(n):
        cfs = _flows(n)
        pv_cashflows_with_dm(bond, curve, cfs, CALC, 250)      # прогрев
        t0 = time.perf_counter()
        for _ in range(10):
            pv_cashflows_with_dm(bond, curve, cfs, CALC, 250)
        return (time.perf_counter() - t0) * 1000

    short, long = ms(36), ms(368)
    assert long < short * 30, f"рост {long / max(short, 1e-6):.0f}× — похоже на квадрат"
