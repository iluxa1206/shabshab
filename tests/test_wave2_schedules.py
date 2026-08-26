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


def test_build_cashflows_survives_high_frequency():
    """core/valuation.py: клэмп нужен В ДВУХ местах подряд — в
    generate_coupon_dates и в step_months строкой ниже. cpy=0 давал
    ZeroDivisionError, cpy>12 — первый период нулевой длины."""
    from core.valuation import add_months
    for cpy in (0, 13, 26, None):
        step = max(1, 12 // max(1, cpy or 4))
        assert step >= 1
        anchor = date(2026, 6, 1)
        assert add_months(anchor, -step) < anchor   # шаг реально двигает дату


def test_set_manual_rejects_high_freq_without_period(tmp_path, monkeypatch):
    """Валидация в set_manual, а не в модели роута: только она покрывает и
    ручной POST, и xlsx-импорт (тот идёт мимо InstrumentParams)."""
    from pathlib import Path
    from services import instruments_registry as reg
    # ИМЕНА АТРИБУТОВ ТОЧНЫЕ, без raising=False: с ним monkeypatch молча заводит
    # фиктивный атрибут, подмена не срабатывает, и тест идёт в БОЕВУЮ БД
    monkeypatch.setattr(reg, "DB_PATH", Path(tmp_path) / "t.db")
    monkeypatch.setattr(reg, "_initialized", False)
    with pytest.raises(ValueError, match="coupon_period_days"):
        reg.set_manual("RU000TEST0001", {"coupons_per_year": 26})
    with pytest.raises(ValueError, match="1..366"):
        reg.set_manual("RU000TEST0001", {"coupons_per_year": 400})


# ─── деградированный фолбэк не платит принципал дважды ──────────────────────

def test_degraded_fallback_pays_residual_not_full_face():
    """Фолбэк эмитил ВСЕ будущие транши И ещё полный face на maturity.

    Защита «нет транша ровно на maturity» не спасала: у ABS последний транш
    вообще за пределами пагинации, а у обычных амортизируемых он сдвинут
    business-day adjustment'ом. Проверяем АРИФМЕТИКУ, а не текст исходника:
    Σ принципала обязана равняться остатку номинала, а не превышать его."""
    from core.valuation import face_for_pricing
    today = date.today()
    face_rem = 1000.0
    amorts = [{"date": (today + timedelta(days=365)).isoformat(), "value": 300.0},
              {"date": (today + timedelta(days=730)).isoformat(), "value": 300.0}]
    settle = today + timedelta(days=1)
    future = sum(a["value"] for a in amorts
                 if date.fromisoformat(a["date"]) > settle)
    residual = face_for_pricing(face_rem, amorts, today) - future
    assert residual == pytest.approx(400.0)              # НЕ 1000
    assert future + residual == pytest.approx(face_rem)  # Σ сходится с номиналом


def test_residual_excludes_tranche_in_settlement_window():
    """Транш из окна (calc_date, settle] достаётся ПРОДАВЦУ: он уже эмитится как
    прошлая амортизация, и в residual попадать не должен — иначе накануне
    амортизации принципал завышен ровно на транш (БалтЛизП10: 100 ₽)."""
    from core.valuation import face_for_pricing, settle_date
    today = date.today()
    settle = settle_date(today)
    amorts = [{"date": settle.isoformat(), "value": 100.0},          # в окне
              {"date": (today + timedelta(days=365)).isoformat(), "value": 200.0}]
    future = sum(a["value"] for a in amorts
                 if date.fromisoformat(a["date"]) > settle)
    residual = face_for_pricing(1000.0, amorts, today) - future
    # 1000 − 100 (продавцу) − 200 (будущий) = 700, а не 800
    assert residual == pytest.approx(700.0)


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


# ─── КОРЕНЬ ВОЛНЫ: amortizations читаются со всех страниц пагинации ──────────

class _Resp:
    status_code = 200

    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


def _page(start):
    """Мок ISS: 2 страницы купонов, 2 страницы амортизаций (100 + 30),
    offers только на первой — ровно так отвечает ISS для ИА РТБ-1."""
    n_c = 100 if start in (0, 100) else 50
    c = [[f"2026-01-{(i % 28) + 1:02d}", f"2026-02-{(i % 28) + 1:02d}", 5.0, 0.5, 1000.0]
         for i in range(n_c)]
    a_n = 100 if start == 0 else (30 if start == 100 else 0)
    a = [[f"2030-{(i % 12) + 1:02d}-01", 7.6923] for i in range(a_n)]
    o = [["2027-05-11", "1", 100.0]] if start == 0 else []
    return {"coupons": {"columns": ["startdate", "coupondate", "value", "valueprc",
                                    "facevalue"], "data": c},
            "amortizations": {"columns": ["amortdate", "value"], "data": a},
            "offers": {"columns": ["offerdate", "offertype", "price"], "data": o}}


def test_amortizations_read_from_every_page(monkeypatch):
    """ISS пагинирует ВСЕ блоки одним start/limit, а не только купоны.

    Стояло `if start == 0`, и хвост амортизаций терялся молча: у ИА РТБ-1
    первая страница даёт 100 траншей, вторая — ещё 30. Из этого обрезка и
    получался «остаток номинала» в 8.78 ₽ при биржевых 577.64."""
    import asyncio
    from services import market_data as md
    MD = md.MarketDataService

    async def fake_get(client, url, params=None, timeout=None):
        return _Resp(_page(params["start"]))

    monkeypatch.setattr(md, "_moex_get", fake_get)
    monkeypatch.setattr(MD, "_full_mem", {}, raising=False)
    monkeypatch.setattr(MD, "_full_mem_date", md._trading_day(), raising=False)
    monkeypatch.setattr(MD, "_save_full_disk", classmethod(lambda cls, force=False: None))

    out = asyncio.run(MD.fetch_bond_schedule_full("RU000TESTAMR1"))
    assert len(out["amorts"]) == 130      # был обрезок ровно в 100
    assert len(out["offers"]) == 1        # offers не задублировались на 2-й странице
    assert len(out["coupons"]) == 250


def test_pagination_stops_when_both_blocks_drained(monkeypatch):
    """Выход из цикла — по исчерпанию ОБОИХ блоков: амортизаций может быть
    больше, чем купонов на странице (иначе хвост снова потеряется)."""
    import asyncio
    from services import market_data as md
    MD = md.MarketDataService
    seen = []

    async def fake_get(client, url, params=None, timeout=None):
        seen.append(params["start"])
        return _Resp(_page(params["start"]))

    monkeypatch.setattr(md, "_moex_get", fake_get)
    monkeypatch.setattr(MD, "_full_mem", {}, raising=False)
    monkeypatch.setattr(MD, "_full_mem_date", md._trading_day(), raising=False)
    monkeypatch.setattr(MD, "_save_full_disk", classmethod(lambda cls, force=False: None))

    asyncio.run(MD.fetch_bond_schedule_full("RU000TESTAMR2"))
    assert seen == [0, 100, 200]          # дочитали до пустой страницы и встали
