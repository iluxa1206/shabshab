"""НКД из протухшего isins_cache не должен молча ехать в расчёт.

Авария 10.09.2026: iss.moex.com лёг на несколько часов, живой снапшот пропал, и
расчёт упал на bond.accrued_rub из isins_cache от 28.07 — полтора месяца назад.
Газпн3P10R получил НКД 6,51 ₽ вместо 10,90 ₽ по графику, Y-IDX выдал 228 bps
против 112 верных при неизменной цене, и НИ ОДНОГО предупреждения: число «не
None», значит источник как бы есть. Ошибка в 4 ₽ на mod_duration 0,35 стоит
~126 bps — и такие числа успели уехать в архив spread_daily.

Купон текущего периода к этому моменту опубликован, то есть график ТОЧЕН.
Расхождение сверх нескольких дней начисления означает протухший кэш.
"""
from datetime import date

from core.valuation import BondRefData
from services.valuation import calculate_valuation_metrics


class _Curve:
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


# Период 16.08-15.09, купон 12,58 ₽ → 0,4193 ₽/день (реальный Газпн3P10R)
PERIODS = [(date(2026, 7, 17), date(2026, 8, 16), 12.68),
           (date(2026, 8, 16), date(2026, 9, 15), 12.58)]
CALC = date(2026, 9, 10)
FAIR_AT_CALC = 12.58 * 25 / 30      # 10,48 ₽ на 10.09


def _bond(accrued):
    return BondRefData(
        isin="RU000A107UW1", base="KEYRATE", spread_issue_bps=130,
        face_value=1000.0, accrued_rub=accrued,
        maturity_date=date(2027, 2, 12), first_coupon_date=date(2024, 3, 29),
        coupons_per_year=12, issue_date=date(2024, 2, 28),
        coupon_period_days=30,
    )


def _calc(cached_accrued, override=None):
    return calculate_valuation_metrics(
        _bond(cached_accrued), 100.0, _Curve(), CALC,
        accrued_override=override, periods=PERIODS)


def test_stale_cache_accrued_replaced_by_schedule():
    stale = _calc(6.51)                       # ровно тот кэш, что был в проде
    assert stale["accrued_estimated"] is True
    assert abs(stale["accrued_settle_rub"] - (FAIR_AT_CALC + 12.58 / 30)) < 0.05
    assert any("разошёлся с графиком" in w for w in stale["warnings"])


def test_stale_accrued_costs_hundred_bps_when_unfixed():
    """Смысл фикса — в цене ошибки. Спред меряем simple margin: он не требует
    базовой кривой и двигается ровно на ошибку НКД.

    override=6,51 воспроизводит СТАРОЕ поведение (сверки нет, число принято как
    биржевое); тот же 6,51 из кэша проходит сверку и чинится."""
    broken = _calc(None, override=6.51)["sm_bps"]        # как считалось до фикса
    fixed = _calc(6.51)["sm_bps"]                        # с фиксом
    honest = _calc(None, override=FAIR_AT_CALC + 12.58 / 30)["sm_bps"]
    assert broken - fixed > 90, "старая ошибка стоила ~100+ bps — тест обязан её видеть"
    assert abs(fixed - honest) < 5, "починенный путь обязан сойтись с биржевым"


def test_settle_gap_within_tolerance_is_kept():
    """Законный разрыв — НКД биржи на свою дату поставки против графика на
    calc_date: 1-3 дня начисления. Его трогать нельзя, иначе фикс переписывал бы
    нормальные биржевые числа своим приближением."""
    near = FAIR_AT_CALC + 2 * (12.58 / 30)     # +2 дня, как T+1 через выходные
    m = _calc(round(near, 2))
    assert m["accrued_estimated"] is False
    assert not any("разошёлся с графиком" in w for w in m["warnings"])


def test_live_snapshot_still_wins_over_cache():
    """Пока ISS жив, override перекрывает кэш и сверка не вмешивается."""
    m = _calc(6.51, override=10.90)
    assert m["accrued_estimated"] is False
    assert abs(m["accrued_settle_rub"] - 10.90) < 0.6
