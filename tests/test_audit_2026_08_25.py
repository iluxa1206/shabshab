"""Регрессы аудита 25.08.2026: провал ставки, rate-limit логина, якорь дюрации,
экранирование имени в боте.

Каждый тест закрывает найденный дефект, а не «поведение вообще» — если правку
откатят, падает ровно он.
"""
from datetime import date, timedelta

import pytest


# ── M2: провал данных → None, а НЕ ставка 0% ───────────────────────────────
def _spec(mode, **kw):
    return {"mode": mode, "lag": 0, "lag_unit": "cal", "base": "KEYRATE", **kw}


@pytest.mark.parametrize("spec", [
    _spec("average"),
    _spec("avg_prev"),
    _spec("point"),
    _spec("month_start"),
    _spec(None, avg_window_days=30),
    _spec(None, avg_window_days=1),
    _spec("average", compounded=True),
])
def test_projected_ks_none_when_no_data(spec):
    """Ни факта ЦБ, ни форварда → None во ВСЕХ режимах.

    Раньше возвращался 0.0 («ставка индекса ноль»), из-за чего bond_audit
    считал err_pp во весь купон и штамповал ложный BAD — отсечка по нулю
    стояла только для mode='point'.
    """
    from services.coupon_calib import projected_ks_pct
    s, e = date(2026, 5, 1), date(2026, 8, 1)
    got = projected_ks_pct(spec, s, e, date(2026, 8, 20),
                           fwd_pct=lambda d: None, idx=([], []))
    assert got is None, f"{spec} вернул {got!r} вместо None"


def test_projected_ks_still_returns_rate_when_data_present():
    """Обратная сторона: при живой истории ставка по-прежнему считается."""
    from services.coupon_calib import projected_ks_pct
    s, e = date(2026, 5, 1), date(2026, 8, 1)
    days = [s - timedelta(days=400) + timedelta(days=i) for i in range(800)]
    idx = (days, [16.0] * len(days))
    got = projected_ks_pct(_spec("average"), s, e, date(2026, 8, 20),
                           fwd_pct=lambda d: None, idx=idx)
    assert got == pytest.approx(16.0, abs=1e-9)


# ── Rate-limit логина: блок отсчитывается от ПОСЛЕДНЕЙ неудачи ─────────────
def test_login_block_does_not_self_disable_after_cap():
    """Регресс обхода: раньше окно считалось от ПЕРВОЙ неудачи, а блок был
    капнут часом — через час `start + block - now` уходил в минус навсегда и
    rate-limit переставал срабатывать при любом числе попыток."""
    from api.routes import auth as A
    A._LOGIN_FAILS.clear()
    t = 1000.0
    for i in range(A._LOGIN_MAX_FAILS):
        A._login_record_fail("k", t + i)
    t5 = t + A._LOGIN_MAX_FAILS - 1
    assert A._login_blocked_for("k", t5 + 1) > 0          # блок наступил

    # спустя час+ атакующий пробует снова: попытка проходит (блок истёк),
    # но счётчик обязан НАКОПИТЬСЯ заново и снова заблокировать
    far = t5 + 3700
    assert A._login_blocked_for("k", far) == 0
    for i in range(A._LOGIN_MAX_FAILS):
        assert A._login_blocked_for("k", far + i) == 0, "блок не должен стоять до порога"
        A._login_record_fail("k", far + i)
    assert A._login_blocked_for("k", far + A._LOGIN_MAX_FAILS) > 0


def test_login_escalates_from_last_attempt():
    """Каждая неудача сверх порога удлиняет блок и начинает его заново."""
    from api.routes import auth as A
    A._LOGIN_FAILS.clear()
    t = 1000.0
    for i in range(A._LOGIN_MAX_FAILS):
        A._login_record_fail("k", t + i)
    last = t + A._LOGIN_MAX_FAILS - 1
    first_block = A._login_blocked_for("k", last)
    # ждём ровно до конца блока и промахиваемся ещё раз
    t2 = last + first_block
    assert A._login_blocked_for("k", t2) == 0
    A._login_record_fail("k", t2)
    assert A._login_blocked_for("k", t2) > first_block


def test_login_success_unblocks():
    """Верный пароль после истечения блока пускает и обнуляет счётчик."""
    from api.routes import auth as A
    A._LOGIN_FAILS.clear()
    t = 1000.0
    for i in range(A._LOGIN_MAX_FAILS):
        A._login_record_fail("k", t + i)
    A._login_reset("k")
    assert A._login_blocked_for("k", t + 10) == 0


# ── M1: якорь дюрации — дата поставки, а не calc_date ──────────────────────
def test_duration_anchored_on_settlement():
    """τ считается от settle (T+1 раб) — как в solve_flat_y, выдавшей y.

    Сдвиг якоря выносится за скобки, поэтому Macaulay смещалась ровно на
    зазор calc→settle: 1 день в будни, 3 в пятницу."""
    from core.valuation import settle_date
    from services.metrics import macaulay_years

    friday = date(2026, 8, 21)
    assert friday.weekday() == 4
    settle = settle_date(friday)
    gap = (settle - friday).days
    assert gap == 3, "ожидаем перенос через выходные"

    cfs = [(friday + timedelta(days=365 * i), 10.0 + (100.0 if i == 3 else 0.0))
           for i in (1, 2, 3)]
    y = 0.16
    mac = macaulay_years(cfs, friday, y)

    # эталон: тот же расчёт с явным якорем на дате поставки
    import math
    num = den = 0.0
    for pay, amt in cfs:
        tau = (pay - settle).days / 365.0
        df = amt * math.exp(-y * tau)
        num += tau * df
        den += df
    assert mac == pytest.approx(num / den, abs=1e-12)


# ── Экранирование имени бумаги в сообщении бота ────────────────────────────
def test_event_name_is_html_escaped():
    """Имя из внешнего справочника с & или < не должно ломать parse_mode=HTML."""
    from api.routes.tg import _fmt_event
    txt = _fmt_event({"name": 'АО «Р&Д» <тест>', "isin": "RU000A100000",
                      "fired_at": "2026-08-25T12:34:56+03:00", "val_bps": 250.0})
    assert "&amp;" in txt and "&lt;тест&gt;" in txt
    assert "<b>" in txt                      # своя разметка не пострадала
    assert "<тест>" not in txt
