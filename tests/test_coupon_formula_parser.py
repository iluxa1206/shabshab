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


def test_fixed_lookback_window_to_avg_prev():
    # «за период Т-37 дня - Т-7 дня» — фикс. окно назад от старта → avg_prev
    # (среднее по окну, лаг = ближний край). Раньше аппроксимировали
    # midpoint-point — врал до 0.5пп (Русагро/РЖД).
    t = ("Ci = RUONIAсрi + S, где RUONIAсрi - среднее значение ставки RUONIA "
         "за период Т-37 дня - Т-7 дня; S = 1.8%.")
    r = P(t)
    assert r["mode"] == "avg_prev" and r["lag"] == 7 and r["lag_unit"] == "cal"


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


def test_glued_predшествующ_no_space():
    # выгрузка Cbonds часто теряет пробел: «предшествующийдате Di» — жадный \w*
    # съедал «дате», якорь фиксинга терялся → mode/lag=null (десятки бумаг)
    t = ("K - значение ключевой ставки на 7-й день, предшествующийдате Di. "
         "Di - Календарная дата i-го купонного периода, увеличенное на 2%")
    r = P(t)
    assert r["mode"] == "average" and r["lag"] == 7


def test_point_do_daty_nachala():
    # «на N рабочий день ДО даты начала» (Cbonds пишет «до», не «предшествующий»)
    t = ("Cr - ключевая ставка ЦБ, действующая по состоянию на 5 рабочий день "
         "до даты начала i-го купонного периода, увеличенная на 3%")
    r = P(t)
    assert r["mode"] == "point" and r["lag"] == 5 and r["lag_unit"] == "work"


def test_point_first_day_of_month():
    t = ("R - ключевая ставка, действующая по состоянию на 1-й (первый) день "
         "календарного месяца, на который приходится дата начала Расчетного периода")
    r = P(t)
    assert r["mode"] == "point"


def test_retro_window_variant_ot_do():
    # «от (Ti-7 до Ti-37)» — та же ретро-конвенция, что «за период Т-37 - Т-7»
    t = "RUONIAсрi = (сумм RUONIAt) / 31 от (Ti-7 до t=Ti-37), где Ti - дата начала"
    r = P(t)
    assert r["mode"] == "avg_prev" and r["lag"] == 7 and r["lag_unit"] == "cal"


def test_cap_procenta_without_percent_sign():
    t = ("Cj = R + S, который не может быть более 21,30 (Двадцати одной целой "
         "и 30/100) процента годовых")
    r = P(t)
    assert r["cap_pct"] == 21.3


def test_ruonia_index_ratio_floater():
    # ВЭБ RUONIA-Индекс ФЛОАТЕР (Р-50/57): купон = (IndexEnd/IndexStart−1)·B/T + S,
    # экономически средняя RUONIA за сдвинутый период → average, лаг 7. max(…;0) —
    # пол 0% (RUONIA>0, не связывает) → capped НЕ ставим.
    t = ("Rj = (max(((Index Endj-7/Index Startj-7) - 1) ; 0) * B/Tj * 100%) + S, "
         "где IndexStart j-7 - значение индекса RUONIA для 7-го календарного дня, "
         "предшествующего Start j; S = 1.85%")
    r = P(t)
    assert r["mode"] == "average" and r["lag"] == 7 and r.get("capped") is None


def test_calibrate_fixed_mode_constrains_pair():
    """fixed_mode: лаг подбирается ПРИ заданном режиме, а не из лучшей чужой пары."""
    from datetime import date, timedelta
    from services.coupon_calib import calibrate, _cache
    _cache.clear()
    # ступенчатый индекс: point(lag=7) и average дают разные ставки
    idx_dates, idx_rates = [], []
    d0 = date(2025, 1, 1)
    for i in range(400):
        idx_dates.append(d0 + timedelta(days=i))
        idx_rates.append(15.0 if i < 200 else 20.0)
    idx = (idx_dates, idx_rates)
    face = 1000.0
    coupons = []
    for k in range(3):
        s = d0 + timedelta(days=120 + 60 * k)
        e = s + timedelta(days=60)
        # факт-купон по point-фиксингу с лагом 7
        rate = (15.0 if (s - timedelta(days=7) - d0).days < 200 else 20.0) + 2.0
        coupons.append({"start": s.isoformat(), "end": e.isoformat(),
                        "value": round(face * rate / 100 * 60 / 365, 2)})
    calc = d0 + timedelta(days=330)
    free = calibrate("T1", coupons, 2.0, face, calc, idx=idx)
    assert free and free["mode"] == "point"
    fixed = calibrate("T1", coupons, 2.0, face, calc, idx=idx, fixed_mode="average")
    # при зажатом average лаг фитится в average-пространстве (или спека None,
    # если фит не проходит порог) — но НЕ пара point-фита
    assert fixed is None or fixed["mode"] == "average"
    # режим вне перебора (avg_prev) → None, дефолты потребителя
    assert calibrate("T1", coupons, 2.0, face, calc, idx=idx, fixed_mode="avg_prev") is None
