"""Тесты parse_prospectus_formula — распознавание режима/лага фиксинга из текста
формулы купона (Cbonds «Купон»). Фокус — окна усреднения RUONIA."""
from services.coupon_calib import parse_prospectus_formula as P, _parse_cache


def setup_function(_):
    _parse_cache.clear()


def test_shifted_period_average_window():
    # «среднее … за период, начинающийся за N дней до даты начала … заканчивающийся
    # за N дней до даты окончания» → окно [s−N, e−N] = _rate_avg(s,e,lag=N).
    t = ("1-28 купоны - среднее арифметическое значение ставок RUONIA за период, "
         "начинающийся за 7 дней до даты начала и заканчивающийся за 7 дней до "
         "даты окончания купонного периода, увеличенное на 1.5%")
    r = P(t)
    assert r["mode"] == "average" and r["lag"] == 7 and r["lag_unit"] == "cal"


def test_shifted_period_working_days():
    t = ("среднее арифметическое ставок RUONIA за период, начинающийся за 5 рабочих "
         "дней до даты начала и заканчивающийся за 5 рабочих дней до даты окончания")
    r = P(t)
    assert r["mode"] == "average" and r["lag"] == 5 and r["lag_unit"] == "work"


def test_fixed_lookback_window_to_midpoint_point():
    # «за период Т-37 дня - Т-7 дня» — фикс. окно назад от старта; гладкий RUONIA
    # ⇒ ≈ точечный фиксинг в середине окна, лаг (37+7)/2 = 22.
    t = ("Ci = RUONIAсрi + S, где RUONIAсрi - среднее значение ставки RUONIA "
         "за период Т-37 дня - Т-7 дня; S = 1.8%.")
    r = P(t)
    assert r["mode"] == "point" and r["lag"] == 22


def test_daily_reset_unchanged():
    # прежнее поведение (петля «предшествующ») не тронуто
    t = ("ставка RUONIA за 7-й день, предшествующий каждой календарной дате, "
         "увеличенная на значение S = 1.9%.")
    r = P(t)
    assert r["mode"] == "average" and r["lag"] == 7


def test_point_keyrate_unchanged():
    t = ("ключевая ставка на 5-й рабочий день, предшествующий дате начала "
         "купонного периода, плюс 2%")
    r = P(t)
    assert r["mode"] == "point" and r["lag"] == 5 and r["lag_unit"] == "work"
