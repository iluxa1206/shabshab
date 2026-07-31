"""Реестр инструментов — единственный источник истины расчётных параметров.

Контракт: правка Справочника (set_manual) немедленно (без TTL-ожидания)
побеждает значения из isins_cache/MOEX в BondRefData всех билдеров.
Регресс: раньше маржа/период/даты парсились из MOEX-дампа (FORMULA/
COUPONPERIOD), и правки реестра в расчёт не попадали вообще.
"""
import importlib
from datetime import date

import pytest


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTRUMENTS_DB", str(tmp_path / "instruments.db"))
    from services import instruments_registry
    importlib.reload(instruments_registry)
    yield instruments_registry
    # вернуть модуль к прод-пути, чтобы не отравить другие тесты
    monkeypatch.delenv("INSTRUMENTS_DB")
    importlib.reload(instruments_registry)


CACHE_ROW = {
    "FACEVALUE": "1000",
    "STARTDATE": "2024-01-10",
    "MATDATE": "2027-01-10",
    "COUPONPERIOD": "91",
    "FREQUENCY": "4",
    "FORMULA": "КС + 2.00%",
    "BASE_RATE": "Ключевая ставка",
}


def test_registry_overrides_isins_cache(reg):
    from services.bonds import create_bond_ref_data

    isin = "RU000TEST0001"
    reg.set_manual(isin, {
        "base": "KEYRATE", "margin_bps": 250, "coupon_period_days": 182,
        "issue_date": "2024-02-01", "maturity_date": "2028-01-10",
        "coupons_per_year": 2, "face_value": 500.0,
    })

    ref = create_bond_ref_data(dict(CACHE_ROW), isin)
    assert ref.spread_issue_bps == 250          # не 200 из FORMULA
    assert ref.coupon_period_days == 182        # не 91 из COUPONPERIOD
    assert ref.issue_date == date(2024, 2, 1)
    assert ref.maturity_date == date(2028, 1, 10)
    assert ref.coupons_per_year == 2
    assert ref.face_value == 500.0


def test_manual_edit_visible_without_ttl_wait(reg):
    from services.bonds import create_bond_ref_data

    isin = "RU000TEST0002"
    reg.set_manual(isin, {"base": "KEYRATE", "margin_bps": 100})
    ref = create_bond_ref_data(dict(CACHE_ROW), isin)
    assert ref.spread_issue_bps == 100

    # повторная правка — кэш calc_params_map обязан сброситься сразу
    reg.set_manual(isin, {"margin_bps": 340})
    ref2 = create_bond_ref_data(dict(CACHE_ROW), isin)
    assert ref2.spread_issue_bps == 340


def test_cache_fallback_when_registry_empty(reg):
    from services.bonds import create_bond_ref_data

    ref = create_bond_ref_data(dict(CACHE_ROW), "RU000TEST0003")
    assert ref.spread_issue_bps == 200          # из FORMULA
    assert ref.coupon_period_days == 91
    assert ref.base == "KEYRATE"


def test_br_layer_beats_parser_but_not_manual(reg, monkeypatch):
    from services import ref_data
    importlib.reload(ref_data)

    isin = "RU000TEST0004"
    # текст формулы, который парсер понимает (point, lag 5)
    reg.upsert({"isin": isin, "base": "KEYRATE", "margin_bps": 200,
                "coupon_text": "КС + 2.0%"}, source="cbonds")
    reg.set_br_spec(isin, 7, "average")
    spec = ref_data.coupon_formula(isin)
    assert spec["coupon_mode"] == "average" and spec["fixing_lag"] == 7  # BR > парсер

    # ручная правка сильнее BR
    reg.set_manual(isin, {"coupon_mode": "point", "fixing_lag": 2})
    spec = ref_data.coupon_formula(isin)
    assert spec["coupon_mode"] == "point" and spec["fixing_lag"] == 2
    importlib.reload(ref_data)


def test_avg_window_days_projection(reg):
    from services.coupon_calib import projected_ks_pct
    from datetime import date, timedelta

    start, end = date(2026, 7, 1), date(2026, 8, 1)
    calc = date(2026, 7, 15)
    # дневная история (иначе стейл-гард _realized уводит дни на форвард):
    # 10% до 24 июня включительно, 20% с 25 июня
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(76)]  # по 15.07
    rates = [10.0 if d < date(2026, 6, 25) else 20.0 for d in dates]
    fwd = lambda d: 99.0   # не должен зваться при полностью известном окне

    # окно 1 день, лаг 3: obs = 28 июня → 20%
    spec = {"mode": None, "lag": 3, "lag_unit": "cal", "base": "KEYRATE",
            "avg_window_days": 1}
    assert projected_ks_pct(spec, start, end, calc, fwd, idx=(dates, rates)) == 20.0

    # окно 10 дней, лаг 0: [21.06, 01.07) → 4 дня по 10% + 6 дней по 20% = 16%
    spec = {"mode": None, "lag": 0, "lag_unit": "cal", "base": "KEYRATE",
            "avg_window_days": 10}
    got = projected_ks_pct(spec, start, end, calc, fwd, idx=(dates, rates))
    assert abs(got - 16.0) < 1e-9


def test_reset_manual(reg):
    isin = "RU000TEST0005"
    reg.set_manual(isin, {"base": "KEYRATE", "margin_bps": 150,
                          "coupon_mode": "point", "fixing_lag": 2,
                          "avg_window_days": 1})
    removed = reg.reset_manual(isin)
    assert removed["coupon_mode"] == "point" and removed["fixing_lag"] == 2
    row = reg.get(isin)
    # спека снята, lock снят — расчётные поля остались
    assert row["coupon_mode"] is None and row["fixing_lag"] is None
    assert row["avg_window_days"] is None and row["manual_locked"] == 0
    assert row["margin_bps"] == 150 and row["base"] == "KEYRATE"
    assert reg.reset_manual("RU000NOSUCH00") is None


def test_future_period_with_realized_window(reg):
    """Будущий период (start > calc), но окно фиксинга уже реализовано
    (avg_window + большой лаг) → купон из ФАКТА истории, не форвард."""
    from services.coupon_calib import period_index_pct
    from datetime import date, timedelta

    isin = "RU000TEST0006"
    reg.set_manual(isin, {"base": "KEYRATE", "margin_bps": 120,
                          "coupon_mode": "average", "fixing_lag": 37,
                          "avg_window_days": 30})
    calc = date(2026, 7, 15)
    start, end = date(2026, 8, 1), date(2026, 9, 1)   # будущий период
    # окно [start-37-30, start-37) = [25.05, 24.06) — полностью в прошлом
    dates = [date(2026, 4, 1) + timedelta(days=i) for i in range(106)]
    rates = [12.0] * len(dates)
    fwd = lambda d: 99.0     # если позовётся — тест упадёт (99 ≠ 12)

    got = period_index_pct(isin, "KEYRATE", [], 1000.0, start, end, calc, fwd,
                           idx=(dates, rates))
    assert got == 12.0

    # KEYRATE point·2, окно не реализовано → спека всё равно применяется:
    # obs = start−2 в будущем → форвард-ступень (99)
    isin2 = "RU000TEST0007"
    reg.set_manual(isin2, {"base": "KEYRATE", "margin_bps": 120,
                           "coupon_mode": "point", "fixing_lag": 2})
    got2 = period_index_pct(isin2, "KEYRATE", [], 1000.0, start, end, calc, fwd,
                            idx=(dates, rates))
    assert got2 == 99.0

    # RUONIA: та же единая методика — спека применяется и к будущим периодам
    # (окно с лагом, факт+форвард-ступени); None только у бумаг без спеки
    isin3 = "RU000TEST0008"
    reg.set_manual(isin3, {"base": "RUONIA", "margin_bps": 120,
                           "coupon_mode": "average", "fixing_lag": 7})
    got3 = period_index_pct(isin3, "RUONIA", [], 1000.0, start, end, calc, fwd,
                            idx=(dates, rates))
    assert got3 is not None and got3 > 90.0     # окно в будущем → форвард (99)

    # бумага БЕЗ спеки: будущий период → None (фолбэк ядра — daily-comp кривой)
    got4 = period_index_pct("RU000NOSPEC000", "RUONIA", [], 1000.0, start, end,
                            calc, fwd, idx=(dates, rates))
    assert got4 is None
