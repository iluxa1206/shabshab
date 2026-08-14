"""Гашение «эха» неопределённых купонов источника.

MOEX bondization у старых ОФЗ-ПК (29006–29010) заполняет ВСЕ будущие купоны
последним известным значением (29010: 16.59% до 2034 года). Ядро трактует
непустой value как факт и не перепрогнозирует его — эхо текло в PV/SM/DM.
Купон, который источник знать НЕ МОГ, обязан считаться нашей логикой
(спека фиксинга + форвард кривой).
"""
from datetime import date

import pytest

from services.coupon_calib import coupon_determined, strip_undetermined_values

TODAY = date(2026, 8, 14)

# ОФЗ 29007 как есть у MOEX: последний купон — эхо предыдущего (17.48%)
OFZ29007 = [
    {"start": "2025-03-05", "end": "2025-09-03", "value": 106.71, "valueprc": 21.40},
    {"start": "2025-09-03", "end": "2026-03-04", "value": 106.51, "valueprc": 21.36},
    {"start": "2026-03-04", "end": "2026-09-02", "value": 87.16, "valueprc": 17.48},
    {"start": "2026-09-02", "end": "2027-03-03", "value": 87.16, "valueprc": 17.48},
]

# спека старых ОФЗ-ПК: среднее RUONIA за 6 месяцев ДО старта периода
SPEC_AVG_PREV = {"mode": "average", "lag": 0, "lag_unit": "cal", "avg_window_days": 182}


def _ends(coupons):
    return {c["end"]: c["value"] for c in coupons}


def test_future_echo_dropped():
    out, dropped = strip_undetermined_values(
        "RU000A0JV4M0", "RUONIA", OFZ29007, TODAY, spec=SPEC_AVG_PREV)
    assert dropped == [date(2027, 3, 3)]
    v = _ends(out)
    assert v["2027-03-03"] is None          # окно [2026-03-02, 2026-09-02) не закрыто
    assert v["2026-09-02"] == 87.16         # текущий купон определён на старте — факт
    assert v["2025-09-03"] == 106.71        # прошлое не трогаем


def test_valueprc_dropped_together():
    out, _ = strip_undetermined_values(
        "RU000A0JV4M0", "RUONIA", OFZ29007, TODAY, spec=SPEC_AVG_PREV)
    last = [c for c in out if c["end"] == "2027-03-03"][0]
    assert last["valueprc"] is None


def test_determined_future_coupon_kept():
    """Лаг больше, чем до старта периода: окно уже закрыто → значение источника
    остаётся фактом (эхо тут ни при чём — ставка другая)."""
    coupons = [
        {"start": "2026-05-15", "end": "2026-08-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-08-15", "end": "2026-11-15", "value": 38.0, "valueprc": 15.2},
    ]
    spec = {"mode": "average", "lag": 7, "lag_unit": "cal", "avg_window_days": 1}
    out, dropped = strip_undetermined_values("X", "KEYRATE", coupons, TODAY, spec=spec)
    assert dropped == []
    assert _ends(out)["2026-11-15"] == 38.0


def test_undetermined_future_dropped_even_without_echo():
    """Окно наблюдения ещё не закрыто → источник знать не мог, гасим независимо
    от того, совпадает ли значение с предыдущим."""
    coupons = [
        {"start": "2026-05-15", "end": "2026-08-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-08-15", "end": "2026-11-15", "value": 38.0, "valueprc": 15.2},
    ]
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "avg_window_days": None}
    out, dropped = strip_undetermined_values("X", "KEYRATE", coupons, TODAY, spec=spec)
    assert dropped == [date(2026, 11, 15)]
    assert _ends(out)["2026-08-15"] == 40.0   # начавшийся период не эхо → факт цел


def test_started_period_kept_when_not_echo():
    """У начавшегося периода факт кормит НКД: ошибочная спека не должна его
    сносить — гасим только при эхе предыдущего значения."""
    coupons = [
        {"start": "2026-02-15", "end": "2026-05-15", "value": 45.0, "valueprc": 18.0},
        {"start": "2026-05-15", "end": "2026-11-15", "value": 40.0, "valueprc": 16.0},
    ]
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "avg_window_days": None}
    out, dropped = strip_undetermined_values("X", "KEYRATE", coupons, TODAY, spec=spec)
    assert dropped == []


def test_started_period_echo_dropped():
    coupons = [
        {"start": "2026-02-15", "end": "2026-05-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-05-15", "end": "2026-11-15", "value": 40.0, "valueprc": 16.0},
    ]
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "avg_window_days": None}
    out, dropped = strip_undetermined_values("X", "KEYRATE", coupons, TODAY, spec=spec)
    assert dropped == [date(2026, 11, 15)]


def test_no_spec_echo_only():
    """Спеки нет — окно посчитать нечем: судим только по эху."""
    coupons = [
        {"start": "2026-05-15", "end": "2026-08-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-08-15", "end": "2026-11-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-11-15", "end": "2027-02-15", "value": 33.0, "valueprc": 13.2},
    ]
    out, dropped = strip_undetermined_values("X", "KEYRATE", coupons, TODAY, spec={})
    assert dropped == [date(2026, 11, 15)]
    assert _ends(out)["2027-02-15"] == 33.0


def test_fixed_bond_untouched():
    """Фикс — не наша юрисдикция: у него будущие купоны известны по определению."""
    coupons = [
        {"start": "2026-05-15", "end": "2026-08-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-08-15", "end": "2026-11-15", "value": 40.0, "valueprc": 16.0},
    ]
    out, dropped = strip_undetermined_values("X", "FIXED", coupons, TODAY)
    assert dropped == [] and out is coupons


def test_amortizing_echo_by_rate():
    """У амортизируемой бумаги равная СТАВКА даёт разные рублёвые value —
    эхо ловится по valueprc, иначе прошло бы мимо."""
    coupons = [
        {"start": "2026-05-15", "end": "2026-08-15", "value": 40.0, "valueprc": 16.0},
        {"start": "2026-08-15", "end": "2026-11-15", "value": 20.0, "valueprc": 16.0},
    ]
    out, dropped = strip_undetermined_values("X", "KEYRATE", coupons, TODAY, spec={})
    assert dropped == [date(2026, 11, 15)]


@pytest.mark.parametrize("spec,start,end,expected", [
    ({"mode": "average", "lag": 7, "lag_unit": "cal", "avg_window_days": 1},
     date(2026, 8, 20), date(2026, 11, 20), True),      # obs 2026-08-13 — в прошлом
    ({"mode": "average", "lag": 0, "lag_unit": "cal", "avg_window_days": None},
     date(2026, 5, 15), date(2026, 11, 15), False),     # окно закроется только к end
    ({"mode": "month_start", "lag": 0, "lag_unit": "cal", "avg_window_days": None},
     date(2026, 8, 20), date(2026, 11, 20), True),
    (None, date(2026, 8, 20), date(2026, 11, 20), None),
])
def test_coupon_determined(spec, start, end, expected):
    assert coupon_determined(spec, start, end, TODAY) is expected
