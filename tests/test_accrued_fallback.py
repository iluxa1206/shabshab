"""НКД: биржевого нет — считаем сам, а не молчим.

До 02.09.2026 отсутствие биржевого НКД было тупиком: dirty_price_rub падал на
None, поэтому потребители точного пути молчали по флагу accrued_missing, и
бумага получала прочерк на весь день. Лестница services/accrued ошибается на
копейки — это на два порядка лучше пустой клетки."""
from datetime import date

import pytest

from core.valuation import BondRefData
from services.valuation import calculate_valuation_metrics


class _Curve:
    """Плоская кривая 20% — форварда хватает и потоку, и оценке НКД."""
    def forward(self, a, b):
        return 0.20

    def rate(self, *a, **k):
        return 0.20

    def zero_rate(self, *a, **k):
        return 0.20

    def df(self, *a, **k):
        return 1.0

    def realized_until(self):
        return date(2026, 9, 1)


def _bond(accrued):
    return BondRefData(
        isin="RU000TESTACC", base="KEYRATE", spread_issue_bps=200,
        face_value=1000.0, accrued_rub=accrued,
        maturity_date=date(2028, 6, 15), first_coupon_date=date(2026, 3, 15),
        coupons_per_year=4, issue_date=date(2025, 12, 15),
        coupon_period_days=91,
    )


def _periods():
    return [(date(2026, 3, 15), date(2026, 6, 15), 50.0),
            (date(2026, 6, 15), date(2026, 9, 15), None)]


def _calc(accrued, override=None, periods=None):
    return calculate_valuation_metrics(
        _bond(accrued), 100.0, _Curve(), date(2026, 9, 1),
        accrued_override=override, periods=periods)


def test_no_accrued_is_computed_not_refused():
    m = _calc(None, periods=_periods())
    assert m["accrued_settle_rub"] and m["accrued_settle_rub"] > 0
    assert m["accrued_estimated"] is True
    assert m["dirty_price_rub"] is not None
    assert any("посчитан сам" in w for w in m["warnings"])


def test_exchange_accrued_wins_and_is_not_flagged():
    m = _calc(None, override=22.7, periods=_periods())
    assert m["accrued_estimated"] is False
    assert abs(m["accrued_settle_rub"] - 22.7) < 1.0     # доначисление до settle


def test_zero_from_source_still_repaired():
    """Старое поведение ветки нуля не сломано: ISS отдаёт ACCRUEDINT=0 посреди
    периода, и «чистая» цена гонит доходность на сотню bps."""
    m = _calc(None, override=0.0, periods=_periods())
    assert m["accrued_estimated"] is True
    assert m["accrued_settle_rub"] > 1.0


def test_nothing_to_compute_with_refuses_instead_of_crashing():
    """Ни биржи, ни расписания, ни параметров: раньше здесь было падение на
    None внутри dirty_price_rub — теперь честный отказ."""
    bare = BondRefData(isin="RU000TESTBARE", base="KEYRATE", spread_issue_bps=0,
                       face_value=1000.0, accrued_rub=None,
                       maturity_date=date(2028, 6, 15),
                       first_coupon_date=None, coupons_per_year=0)
    m = calculate_valuation_metrics(bare, 100.0, _Curve(), date(2026, 9, 1))
    assert m["pricing_status"] == "NO_ACCRUED"
    assert m["dirty_price_rub"] is None
    assert m["yield_over_index_bps"] is None


def test_exact_path_no_longer_silent_without_exchange_accrued():
    """yidx_exact молчал по флагу accrued_missing — ровно это и был прочерк."""
    from services.yidx_exact import y_idx_many
    ctx = {"isin": "RU000TESTACC", "ref_obj": _bond(None), "curve": _Curve(),
           "ruonia_curve": _Curve(), "calc_date": date(2026, 9, 1),
           "accrued_live": None, "accrued_date": None,
           "periods": _periods(), "amorts": None, "offers": None,
           "accrued_missing": True}
    got = y_idx_many(ctx, [100.0, 99.5])
    # До фикса здесь был ПУСТОЙ словарь: функция выходила по accrued_missing, не
    # доходя до расчёта, — витрина показывала прочерк. Теперь цены доезжают до
    # движка; сами числа на этой заглушке кривой не считаются (Y-IDX требует
    # RUONIA-кривую с историей), и проверять их тут нечего — важно, что путь
    # открыт. Что НКД при этом взялся из лестницы, проверяет тест выше.
    assert set(got) == {100.0, 99.5}
