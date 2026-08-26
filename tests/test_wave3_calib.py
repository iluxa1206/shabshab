"""Регресс-щит волны 3 аудита 2026-08-26: калибровка купонов флоатеров.

Особенность волны: каждый предложенный аудитом патч ломал тесты или прод —
все пять фиксов балансируют между двумя одинаково выглядящими случаями."""
from datetime import date, timedelta

import pytest


# ─── праздничная пауза vs дыра источника ────────────────────────────────────

def test_holiday_gap_vs_source_hole():
    """ПО ДЛИНЕ НЕ РАЗЛИЧИТЬ: новогодняя пауза RUONIA — 13-14 дней, дыра
    источника (регресс Росагролизинга, RC_F до 08.07 / live с 21.07) — 13.
    Различаем по производственному календарю: в праздник рабочих дней ≤2."""
    from services.coupon_calib import _holiday_gap
    # новогодние каникулы: рабочих дней внутри почти нет
    assert _holiday_gap(date(2024, 12, 27), date(2025, 1, 9)) is True
    # майские
    assert _holiday_gap(date(2025, 4, 30), date(2025, 5, 5)) is True
    # сбой источника в июле: две полные рабочие недели
    assert _holiday_gap(date(2026, 7, 8), date(2026, 7, 21)) is False


def test_realized_accepts_holiday_hole():
    """День внутри праздничной паузы — РЕАЛИЗОВАННЫЙ факт (carry-forward
    корректен по конвенции выпусков), а не повод уходить в форвард."""
    from services.coupon_calib import _realized
    dts = [date(2024, 12, 27), date(2025, 1, 9), date(2025, 1, 10)]
    idx = (dts, [21.0] * len(dts))
    calc = date(2025, 6, 1)
    assert _realized(idx, date(2025, 1, 3), calc) is True     # внутри каникул
    assert _realized(idx, date(2024, 12, 27), calc) is True   # сама точка


def test_realized_rejects_source_hole():
    """А дыра источника той же длины — НЕ факт: там нельзя тянуть ставку."""
    from services.coupon_calib import _realized
    dts = [date(2026, 7, 8), date(2026, 7, 21)]
    idx = (dts, [21.0, 21.0])
    assert _realized(idx, date(2026, 7, 15), date(2026, 9, 1)) is False


def test_realized_tail_still_strict():
    """ХВОСТ ряда (публикация встала) — по-прежнему строгий grace: застой
    индекса не должен молча течь в купон."""
    from services.coupon_calib import _realized
    dts = [date(2026, 7, 1)]
    idx = (dts, [21.0])
    calc = date(2026, 9, 1)
    assert _realized(idx, date(2026, 7, 3), calc) is True      # в пределах grace
    assert _realized(idx, date(2026, 8, 20), calc) is False    # застой


def test_projection_equals_calibrator_on_past_window():
    """ИНВАРИАНТ комментария coupon_calib.py:431: калибратор и проекция обязаны
    мерить одинаково. Период ЦЕЛИКОМ в прошлом (ровно то, чем питается
    calibrate) — projected_ks_pct не должна ни разу позвать fwd_pct.

    Маркер 999 в результате = форвард протёк в прошлое (core/forwards клампит
    lo = max(d, calc_date), поэтому прошлая дата получает СЕГОДНЯШНИЙ сегмент)."""
    from services.coupon_calib import projected_ks_pct, _rate_avg
    dts = ([date(2024, 12, 20) + timedelta(days=i) for i in range(8)]
           + [date(2025, 1, 9) + timedelta(days=i) for i in range(20)])
    idx = (dts, [21.0] * len(dts))
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "base": "RUONIA"}
    s, e = date(2024, 12, 22), date(2025, 1, 25)
    got = projected_ks_pct(spec, s, e, date(2025, 6, 1),
                           fwd_pct=lambda d: 999.0, idx=idx)
    ref = _rate_avg(idx, s, e, 0)
    assert abs(got - ref) < 1e-9, f"форвард протёк: {got} против {ref}"


# ─── off-by-one у точечного фиксинга ────────────────────────────────────────

def test_last_obs_date_matches_probe():
    """ref_data нормализует ВСЕ point-бумаги в average+W=1 (152 выпуска), и
    голый `hi − 1` объявлял купон определённым за день до фиксинга.
    _last_obs_date и fixing_probe_date обязаны совпадать."""
    from services.coupon_calib import _last_obs_date, fixing_probe_date
    start = date(2026, 9, 1)
    for w in (1, 2, 30):
        spec = {"mode": "average", "lag": 7, "lag_unit": "cal", "avg_window_days": w}
        a = _last_obs_date(spec, start, start + timedelta(days=90))
        b = fixing_probe_date(spec, start)
        assert a == b, f"W={w}: {a} != {b}"


def test_point_window_last_obs_is_obs_itself():
    """W=1 ≡ точечный фиксинг: единственное наблюдение — сам obs(start)."""
    from services.coupon_calib import _last_obs_date, _obs_date
    start = date(2026, 9, 1)
    spec = {"mode": "average", "lag": 7, "lag_unit": "cal", "avg_window_days": 1}
    assert _last_obs_date(spec, start, start + timedelta(days=90)) == \
        _obs_date(start, 7, "cal")


# ─── эхо-правило ────────────────────────────────────────────────────────────

def _sched(today, started_rate, future_rate):
    """Начавшийся период + будущий, с заданными ставками."""
    return [
        {"start": (today - timedelta(days=120)).isoformat(),
         "end": (today - timedelta(days=30)).isoformat(),
         "value": 50.0, "valueprc": started_rate},
        {"start": (today - timedelta(days=30)).isoformat(),      # НАЧАВШИЙСЯ
         "end": (today + timedelta(days=60)).isoformat(),
         "value": 50.0, "valueprc": started_rate},
        {"start": (today + timedelta(days=60)).isoformat(),      # будущий
         "end": (today + timedelta(days=150)).isoformat(),
         "value": 50.0, "valueprc": future_rate},
    ]


def test_started_coupon_kept_without_spec():
    """Равные подряд ставки при НЕИЗВЕСТНОЙ спеке — норма, а не эхо MOEX:
    плоская КС, лесенка «фиксируется на каждые 4 купона» (АРАГОН об),
    fix-to-float прелюдия (ТАЛК002P04: 12 купонов по 24%). Значение
    начавшегося купона кормит НКД — сносить его по догадке нельзя."""
    from services.coupon_calib import strip_undetermined_values
    today = date.today()
    out, dropped = strip_undetermined_values(
        "RU000ECHO0001", "KEYRATE", _sched(today, 24.0, 24.0), today, None)
    started_end = today + timedelta(days=60)
    assert started_end not in dropped, "начавшийся купон погашен догадкой"


def test_future_echo_still_dropped():
    """А для БУДУЩИХ купонов правило сохраняется полностью: модельный купон
    безопаснее эха (у ОФЗ-ПК 29010 оно тянулось до 2034 года)."""
    from services.coupon_calib import strip_undetermined_values
    today = date.today()
    out, dropped = strip_undetermined_values(
        "RU000ECHO0002", "KEYRATE", _sched(today, 24.0, 24.0), today, None)
    future_end = today + timedelta(days=150)
    assert future_end in dropped, "будущее эхо не погашено"


# ─── гейт калибратора ───────────────────────────────────────────────────────

def test_calibrate_rejects_flat_index():
    """Плоский индекс: err=0 значит НЕРАЗЛИЧИМО, а не верно. Два купона фитят
    что угодно."""
    from services.coupon_calib import calibrate
    dts = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]
    idx = (dts, [21.0] * len(dts))     # индекс не двигался
    # купон = (ставка+маржа)/100 × номинал × дней/365
    cps = [{"start": s.isoformat(), "end": e.isoformat(),
            "value": round(1000.0 * 0.22 * (e - s).days / 365.0, 2)}
           for s, e in ((date(2025, 3, 1), date(2025, 6, 1)),
                        (date(2025, 6, 1), date(2025, 9, 1)),
                        (date(2025, 9, 1), date(2025, 12, 1)))]
    got = calibrate("RU000FLAT0001", cps, 1.0, 1000.0, date(2026, 1, 15),
                    base="KEYRATE", idx=idx)
    assert got is None, f"плоский индекс дал спеку: {got}"


def test_calibrate_rejects_two_coupons():
    """Меньше трёх наблюдений — фитить нечего."""
    from services.coupon_calib import calibrate
    dts = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]
    rates = [16.0 if d < date(2025, 6, 1) else 21.0 for d in dts]
    idx = (dts, rates)     # размах 5 пп — дело только в числе купонов
    cps = [{"start": s.isoformat(), "end": e.isoformat(),
            "value": round(1000.0 * (r + 1.0) / 100.0 * (e - s).days / 365.0, 2)}
           for s, e, r in ((date(2025, 3, 1), date(2025, 6, 1), 16.0),
                           (date(2025, 6, 1), date(2025, 9, 1), 21.0))]
    got = calibrate("RU000TWO00001", cps, 1.0, 1000.0, date(2026, 1, 15),
                    base="KEYRATE", idx=idx)
    assert got is None, f"два купона дали спеку: {got}"
