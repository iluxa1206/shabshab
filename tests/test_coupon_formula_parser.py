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


def test_month_start_first_day_of_month():
    # фиксинг на 1-е число месяца старта периода (ИЖА ДОМ.РФ) — точный режим,
    # а не аппроксимация point lag 0 (давала систематику ~0.33пп)
    t = ("R - ключевая ставка, действующая по состоянию на 1-й (первый) день "
         "календарного месяца, на который приходится дата начала Расчетного периода")
    r = P(t)
    assert r["mode"] == "month_start"
    assert r["lag"] == 0


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


# ── month_start: проекция фиксинга на 1-е число месяца ──────────────────────

def test_month_start_projection_uses_month_first_day():
    from datetime import date, timedelta
    from services.coupon_calib import projected_ks_pct
    # индекс меняется 10-го числа: 1-е число месяца = 15%, старт периода (20-е) = 20%
    d0 = date(2025, 6, 1)
    idx_dates = [d0 + timedelta(days=i) for i in range(60)]
    idx_rates = [15.0 if (d0 + timedelta(days=i)).day < 10 or
                 (d0 + timedelta(days=i)).month == 6 else 15.0 for i in range(60)]
    # проще: июнь весь 15%, с 10 июля 20%
    idx_rates = []
    for d in idx_dates:
        idx_rates.append(20.0 if (d.month, d.day) >= (7, 10) else 15.0)
    spec = {"mode": "month_start", "lag": 0, "lag_unit": "cal", "base": "KEYRATE"}
    s, e = date(2025, 7, 20), date(2025, 8, 20)
    got = projected_ks_pct(spec, s, e, date(2025, 7, 25),
                           fwd_pct=lambda d: 99.0, idx=(idx_dates, idx_rates))
    # фиксинг на 1 июля (15%), а не на старт 20 июля (20%)
    assert got == 15.0


def test_fixing_probe_date_month_start():
    from datetime import date
    from services.coupon_calib import fixing_probe_date
    assert fixing_probe_date({"mode": "month_start"}, date(2025, 7, 20)) == date(2025, 7, 1)
    assert fixing_probe_date({"mode": "point", "lag": 3, "lag_unit": "cal"},
                             date(2025, 7, 20)) == date(2025, 7, 17)


# ── parse_margin_schedule: лесенка маржи по номерам купонов ─────────────────

def test_margin_schedule_s_ranges():
    from services.coupon_calib import parse_margin_schedule
    t = ("1-21 купоны: RDI = K + S, гдеК - значение ключевой ставки Банка России "
         "(в процентах годовых) на 7-й (седьмой) день,предшествующий дате Di. "
         "S - надбавка, в процентах годовых. S 1-7 = 2.5%, S8-21 = 4.6%")
    assert parse_margin_schedule(t) == [
        {"from": 1, "to": 7, "bps": 250}, {"from": 8, "to": 21, "bps": 460}]


def test_margin_schedule_coupon_ranges_with_plus():
    from services.coupon_calib import parse_margin_schedule
    t = ("1-12 купоны - 9.8% годовых, 13-18 купоны: Ci = R + 2,5%, где Сi - "
         "процентная ставка; 19-22 купоны:Ci = R + 3,5%")
    # фикс-ступень 1-12 (без «+») не попадает; плавающие диапазоны — попадают
    assert parse_margin_schedule(t) == [
        {"from": 13, "to": 18, "bps": 250}, {"from": 19, "to": 22, "bps": 350}]


def test_margin_schedule_zero_margin_significant():
    from services.coupon_calib import parse_margin_schedule
    t = "6-8 купоны - Ключевая ставка ЦБ РФ + 0% годовых"
    assert parse_margin_schedule(t) == [{"from": 6, "to": 8, "bps": 0}]


def test_margin_schedule_letter_indexed():
    from services.coupon_calib import parse_margin_schedule
    t = ("1 купон- 22% годовых, 2-36 купоны:Ci = MIN(Cr+6,0%; 24%)"
         "Cy = MIN(Cr+5,0%; 23%)Ck = MIN(Cr+4,0%; 22%), гдеi, y, k - порядковые "
         "номера купонных периодов, при этом i = 2, 3...12; y = 13, 14...24; "
         "k = 25, 26...36.")
    # буквенная привязка бьёт наивный range-матч «2-36 → +6%»
    assert parse_margin_schedule(t) == [
        {"from": 2, "to": 12, "bps": 600}, {"from": 13, "to": 24, "bps": 500},
        {"from": 25, "to": 36, "bps": 400}]


def test_margin_schedule_cpi_linker_none():
    from services.coupon_calib import parse_margin_schedule
    t = "2-5 купоны - Cj = MAX ((Ij -100%) + 4%; Gj + 1%), где Ij - индекс потребительских цен"
    assert parse_margin_schedule(t) is None


def test_margin_schedule_symbolic_margin_skipped():
    from services.coupon_calib import parse_margin_schedule
    # символьная надбавка «+ f» без числа рядом — не диапазон
    t = "1-119 купоны - Ci = R + f, где: Ci - размер ставки; f - 1.7%."
    assert parse_margin_schedule(t) is None


def test_average_window_income_days_convention():
    """НКД-конвенция дней дохода: average усредняет obs по дням (s, e], не [s, e).
    Сверка до копейки с эмитентом (БалтЛизП10): [s, e) сдвигал фиксинги на −1д."""
    from datetime import date, timedelta
    from services.coupon_calib import projected_ks_pct
    d0 = date(2025, 1, 1)
    idx_dates = [d0 + timedelta(days=i) for i in range(120)]
    # ступень: до 2025-02-10 = 10%, дальше 20%
    idx_rates = [10.0 if d < date(2025, 2, 10) else 20.0 for d in idx_dates]
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "base": "KEYRATE"}
    s, e = date(2025, 2, 9), date(2025, 2, 19)  # 10 дней дохода: 10..19 февраля
    got = projected_ks_pct(spec, s, e, date(2025, 4, 1),
                           fwd_pct=lambda d: None, idx=(idx_dates, idx_rates))
    # дни дохода 10-19 фев — ВСЕ по 20%; конвенция [s, e) включила бы 9 фев (10%)
    assert got == 20.0


# ── parse_margin_schedule_field: РУЧНАЯ лесенка из Справочника ──────────────

def test_margin_schedule_field_compact_and_json():
    from services.coupon_calib import parse_margin_schedule_field as f
    assert f("7-20=400") == [{"from": 7, "to": 20, "bps": 400}]
    assert f("1-7=250; 8-21=460") == [{"from": 1, "to": 7, "bps": 250},
                                      {"from": 8, "to": 21, "bps": 460}]
    # одиночный купон и JSON-форма дают то же самое
    assert f("9=0") == [{"from": 9, "to": 9, "bps": 0}]
    assert f('[{"from":7,"to":20,"bps":400}]') == [{"from": 7, "to": 20, "bps": 400}]
    assert f("") is None and f(None) is None


def test_margin_schedule_field_rejects_garbage_and_overlap():
    import pytest
    from services.coupon_calib import parse_margin_schedule_field as f
    with pytest.raises(ValueError):
        f("8-21=460; 1-9=250")      # пересечение диапазонов
    with pytest.raises(ValueError):
        f("КС + 4%")               # не лесенка — молча проглотить нельзя
    with pytest.raises(ValueError):
        f("20-7=400")              # перевёрнутый диапазон


def test_margin_schedule_manual_beats_parser_on_other_base_steps():
    """Ситиматик RU000A0JU9K4: купоны 2-6 — MAX(инфляция+4%; ставка
    рефинансирования+1%), т.е. НЕ КС. parse_margin_schedule сознательно молчит,
    и КС-часть («7-20 купоны — КС + 4%») задаётся руками."""
    from services.coupon_calib import (parse_margin_schedule,
                                       parse_margin_schedule_field)
    t = ("1 купон - 11% годовых, 2-6 купоны - большая из величин: инфляция плюс 4% "
         "или ставка рефинансирования ЦБ плюс 1%, 7-20 купоны - Ключевая ставка "
         "Банка России + 4%.")
    assert parse_margin_schedule(t) is None
    assert parse_margin_schedule_field("7-20=400") == [{"from": 7, "to": 20, "bps": 400}]
