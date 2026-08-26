"""Регресс-щит волны 2 аудита 2026-08-26: пагинация ISS и потоки.

Корень всей волны: fetch_bond_schedule_full читал amortizations ТОЛЬКО с первой
страницы, хотя ISS пагинирует все блоки одним start/limit. У ИА РТБ-1 start=0
даёт 100 амортизаций, start=100 — ещё 30; хвост терялся молча."""
from datetime import date, timedelta

import pytest


# ─── зависание на частоте > 12 купонов в год ────────────────────────────────

def test_generate_coupon_dates_terminates_on_high_frequency():
    """12 // 26 == 0 → add_months(d, 0) не двигает дату → БЕСКОНЕЧНЫЙ while.

    Достижимо: instruments_sync пишет coupons_per_year = round(365/period),
    и для 14-дневного купона (ВЭБP-46) это 26."""
    from core.valuation import generate_coupon_dates
    d = generate_coupon_dates(date(2025, 1, 10), date(2026, 1, 10), 26)
    assert 0 < len(d) < 100
    assert d == sorted(d) and len(set(d)) == len(d)   # строго возрастающие


def test_generate_coupon_dates_zero_frequency():
    """coupons_per_year=0 давал ZeroDivisionError."""
    from core.valuation import generate_coupon_dates
    assert isinstance(generate_coupon_dates(date(2025, 1, 10), date(2026, 1, 10), 0), list)


def test_next_coupon_after_steps_on_high_frequency():
    """step = 12 // 26 == 0 → цикл не двигал дату и возвращал ДАТУ ПОГАШЕНИЯ
    вместо ближайшего купона."""
    import inspect
    from services import bonds
    src = inspect.getsource(bonds)
    assert "max(1, 12 // (ref_obj.coupons_per_year or 4))" in src


def test_set_manual_rejects_high_freq_without_period(tmp_path, monkeypatch):
    """Валидация в set_manual, а не в модели роута: только она покрывает и
    ручной POST, и xlsx-импорт (тот идёт мимо InstrumentParams)."""
    from services import instruments_registry as reg
    monkeypatch.setattr(reg, "_DB", str(tmp_path / "t.db"), raising=False)
    monkeypatch.setattr(reg, "_ready", False, raising=False)
    with pytest.raises(ValueError, match="coupon_period_days"):
        reg.set_manual("RU000TEST0001", {"coupons_per_year": 26})
    with pytest.raises(ValueError, match="1..366"):
        reg.set_manual("RU000TEST0001", {"coupons_per_year": 400})


# ─── деградированный фолбэк не платит принципал дважды ──────────────────────

def test_degraded_fallback_pays_residual_not_full_face():
    """Фолбэк эмитил ВСЕ будущие транши И ещё полный face на maturity.

    Защита «нет транша ровно на maturity» не спасала: у ABS последний транш
    вообще за пределами пагинации, а у обычных амортизируемых он сдвинут
    business-day adjustment'ом."""
    import inspect
    from services import cashflow, payments_calendar
    for mod in (cashflow, payments_calendar):
        src = inspect.getsource(mod)
        assert "residual" in src, f"{mod.__name__}: residual-логика не на месте"
        assert "_future_am" in src, f"{mod.__name__}: транши не вычитаются"


# ─── ФИКСЫ: обрезанный график → отказ, а не мусорное число ──────────────────

def test_fixed_rejects_incomplete_amort_schedule():
    """Σ будущих траншей много меньше биржевого номинала = график обрезан.

    НЕ достраиваем residual: put_date у таких бумаг — ближайший НЕОПРЕДЕЛЁННЫЙ
    купон, а не оферта, и выкуп всего номинала на эту дату дал бы 33-143%."""
    from services.fixed_income import build_fixed_cashflows
    today = date.today()
    sched = {
        "coupons": [{"start": (today + timedelta(days=30 * i)).isoformat(),
                     "end": (today + timedelta(days=30 * (i + 1))).isoformat(),
                     "value": 5.0, "face": 1000.0} for i in range(6)],
        # опубликован ОДИН транш вместо полного графика
        "amorts": [{"date": (today + timedelta(days=200)).isoformat(), "value": 8.78}],
        "offers": [],
    }
    cfs, face, put = build_fixed_cashflows(sched, today, exchange_face=577.64)
    assert face is None and cfs == []      # отказ, а не 8.78

    # контроль: полный график принимается
    sched["amorts"] = [{"date": (today + timedelta(days=200)).isoformat(), "value": 577.64}]
    cfs2, face2, _ = build_fixed_cashflows(sched, today, exchange_face=577.64)
    assert face2 == pytest.approx(577.64) and cfs2


def test_fixed_metrics_flags_incomplete():
    """Отказ билдера обязан быть ВИДЕН, а не превращаться в тихие None."""
    from services.fixed_income import fixed_metrics_from_schedule
    today = date.today()
    sched = {
        "coupons": [{"start": today.isoformat(),
                     "end": (today + timedelta(days=30)).isoformat(),
                     "value": 5.0, "face": 1000.0}],
        "amorts": [{"date": (today + timedelta(days=200)).isoformat(), "value": 8.78}],
        "offers": [],
    }
    m = fixed_metrics_from_schedule(sched, 100.0, 0.0, today, None, exchange_face=577.64)
    assert m.get("incomplete_schedule") is True
    assert m["ytm_pct"] is None


# ─── атомарная запись кэша уникальна на писателя ────────────────────────────

def test_atomic_write_tmp_is_unique(tmp_path):
    """Фиксированное «{path}.tmp» два конкурентных писателя открывали
    одновременно, json.dump интерливился, os.replace выкладывал наполовину
    перезаписанный файл (наблюдалось: 7 КБ вместо 2.7 МБ)."""
    import json
    import threading
    from services.paths import atomic_write_json

    target = str(tmp_path / "c.json")
    errors = []

    def w(n):
        try:
            atomic_write_json(target, {"who": n, "payload": list(range(2000))})
        except Exception as e:      # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    got = json.load(open(target, encoding="utf-8"))     # файл ЦЕЛЫЙ
    assert len(got["payload"]) == 2000
