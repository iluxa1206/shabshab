"""Кэш пути роста RUONIA-индекса: бит-в-бит с прямым _index_grow.

_index_grow_cached обязан давать РОВНО тот же уровень, что построчная
рекуррентность _index_grow — купоны RUONIA-бумаг калиброваны к ряду ЦБ до
десятых bps, любое расхождение кэша сломало бы сверку. Плюс инвалидация: смена
кривой (fwd_pct), дозапись истории и другой calc_date сбрасывают путь.
"""
from datetime import date, timedelta

import pytest

from services import coupon_calib as cc


@pytest.fixture(autouse=True)
def clean_cache():
    cc._GROW_PATH.update(key=None, levels=None, state=None)
    yield
    cc._GROW_PATH.update(key=None, levels=None, state=None)


CALC = date(2026, 8, 5)
LAST_OFF = date(2026, 8, 1)


def _idx(last=date(2026, 8, 3), n=400, rate=15.0):
    """Синтетический ряд фиксингов: n дней до last включительно."""
    dates = [last - timedelta(days=i) for i in range(n)][::-1]
    rates = [rate + (i % 7) * 0.01 for i in range(n)]
    return (dates, rates)


def _fwd(level=16.0):
    return lambda d: level + (d.toordinal() % 5) * 0.02


@pytest.mark.parametrize("horizon_days", [1, 3, 30, 365, 1100])
def test_cached_equals_direct(horizon_days):
    idx, fwd = _idx(), _fwd()
    to = LAST_OFF + timedelta(days=horizon_days)
    direct = cc._index_grow(LAST_OFF, to, CALC, fwd, idx)
    cached = cc._index_grow_cached(LAST_OFF, to, CALC, fwd, idx)
    assert cached == pytest.approx(direct, abs=1e-14)


def test_incremental_extension_exact():
    """Ближний запрос, потом дальний: продолжение пути = прямой расчёт."""
    idx, fwd = _idx(), _fwd()
    near = LAST_OFF + timedelta(days=40)
    far = LAST_OFF + timedelta(days=900)
    cc._index_grow_cached(LAST_OFF, near, CALC, fwd, idx)
    cached_far = cc._index_grow_cached(LAST_OFF, far, CALC, fwd, idx)
    assert cached_far == pytest.approx(cc._index_grow(LAST_OFF, far, CALC, fwd, idx), abs=1e-14)
    # и промежуточная точка после расширения — тоже точная
    mid = LAST_OFF + timedelta(days=200)
    assert cc._index_grow_cached(LAST_OFF, mid, CALC, fwd, idx) == \
        pytest.approx(cc._index_grow(LAST_OFF, mid, CALC, fwd, idx), abs=1e-14)


def test_second_call_is_dict_hit():
    idx, fwd = _idx(), _fwd()
    to = LAST_OFF + timedelta(days=365)
    cc._index_grow_cached(LAST_OFF, to, CALC, fwd, idx)
    state_before = cc._GROW_PATH["state"]
    cc._index_grow_cached(LAST_OFF, to, CALC, fwd, idx)
    assert cc._GROW_PATH["state"] is state_before   # путь не перестраивался


def test_curve_change_invalidates():
    """Другая кривая → другой путь, не отравленный старым кэшем."""
    idx = _idx()
    to = LAST_OFF + timedelta(days=200)
    a = cc._index_grow_cached(LAST_OFF, to, CALC, _fwd(16.0), idx)
    b = cc._index_grow_cached(LAST_OFF, to, CALC, _fwd(11.0), idx)
    assert a != b
    assert b == pytest.approx(cc._index_grow(LAST_OFF, to, CALC, _fwd(11.0), idx), abs=1e-14)


def test_index_append_invalidates():
    """Дозапись фиксинга ЦБ (новый день истории) сбрасывает путь."""
    fwd = _fwd()
    to = LAST_OFF + timedelta(days=200)
    a = cc._index_grow_cached(LAST_OFF, to, CALC, fwd, _idx(last=date(2026, 8, 3)))
    idx2 = _idx(last=date(2026, 8, 4), n=401)
    b = cc._index_grow_cached(LAST_OFF, to, CALC, fwd, idx2)
    assert b == pytest.approx(cc._index_grow(LAST_OFF, to, CALC, fwd, idx2), abs=1e-14)


def test_calc_date_change_invalidates():
    """Смена calc_date двигает границу факт/форвард — путь обязан пересобраться."""
    idx, fwd = _idx(), _fwd()
    to = LAST_OFF + timedelta(days=100)
    cc._index_grow_cached(LAST_OFF, to, CALC, fwd, idx)
    key1 = cc._GROW_PATH["key"]
    cc._index_grow_cached(LAST_OFF, to, CALC + timedelta(days=1), fwd, idx)
    assert cc._GROW_PATH["key"] != key1
