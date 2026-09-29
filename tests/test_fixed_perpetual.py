"""ФИКСЫ: бессрочные и валютные бумаги давали отрицательный мусор в YTM.

Снимок прода 29.09.2026 — 75 бумаг с YTM < 0 при цене < 100, две причины:

1. БЕССРОЧНЫЕ (MOEX MATDATE '0000-00-00'). В графике bondization нет ни
   траншей погашения, ни неопределённых купонов, и в XIRR уходили одни
   известные купоны без номинала: ВТБСУБ1-11 при цене 63 — YTM −99.4 против
   15.87 у MOEX. Теперь это рента: купоны графика + последний известный купон,
   продлённый вперёд. MOEX показывает купон / цена (простая), у нас та же
   рента, но эффективная годовая — как весь остальной YTM витрины.

2. ВАЛЮТНЫЕ (USD/EUR/CNY). Универс собран с TQCB, где расчёты рублёвые, и
   ACCRUEDINT там в РУБЛЯХ: НорНикЗ26Д — 1004.45 при номинале 1000 USD.
   Сложенный с ценой в % от валютного номинала, НКД удваивал dirty (−99.99).
   НКД валютной бумаги теперь из её графика купонов, в валюте номинала.
"""
from datetime import date

import pytest

from core.valuation import settle_date
from services import fixed_income as fi


CD = date(2026, 9, 29)   # вторник → поставка 30.09


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: None)
    fi.clear_price_metrics_cache()


# ВТБСУБ1-11 (RU000A103SB8), хвост реального графика MOEX: полугодовой купон
# 10 % на номинал 10 млн, график кончается 29.03.2027, амортизаций нет.
VTB_SUB = {
    "coupons": [
        {"start": "2025-09-29", "end": "2026-03-30", "value": 498630.14, "face": 10_000_000},
        {"start": "2026-03-30", "end": "2026-09-28", "value": 498630.14, "face": 10_000_000},
        {"start": "2026-09-28", "end": "2027-03-29", "value": 498630.14, "face": 10_000_000},
    ],
    "amorts": [],
    "offers": [{"date": "0000-00-00", "type": "Оферта/Погашение", "price": None}],
}


def _vtb_row(**kw):
    row = {"isin": "RU000A103SB8", "name": "ВТБСУБ1-11", "maturity_date": None,
           "perpetual": True, "faceunit": "SUR", "face": 10_000_000.0,
           "coupon_pct": 10.0, "accrued": 5479.45, "last": 63.0, "prev": 63.0}
    row.update(kw)
    return row


def _perpetuity_yield(dirty: float, coupon: float, first_pay: date, settle: date,
                      step_years: float) -> float:
    """Независимая сверка: рента в закрытой форме
    PV = C·v^t1 / (1 − v^Δ), v = 1/(1+y), бисекцией по y."""
    t1 = (first_pay - settle).days / 365.0
    lo, hi = 1e-6, 5.0
    for _ in range(200):
        y = (lo + hi) / 2
        v = 1.0 / (1.0 + y)
        pv = coupon * v ** t1 / (1.0 - v ** step_years)
        lo, hi = (y, hi) if pv > dirty else (lo, y)
    return (lo + hi) / 2


def test_perpetual_ytm_is_perpetuity_not_garbage():
    out = fi.compute_fixed_row(_vtb_row(), VTB_SUB, None, CD)
    # было −99.4: поток без номинала
    assert out["ytm"] is not None and out["ytm"] > 0
    ref = _perpetuity_yield(out["dirty"], 498630.14, date(2027, 3, 29),
                            settle_date(CD), 0.5) * 100
    # хвост потока за 100 лет весит (1.16)^-100 ≈ 4e-7 цены — сверка до сотых
    assert out["ytm"] == pytest.approx(ref, abs=0.02)
    # простая «купон / цена» MOEX (15.87) — отдельная колонка; YTM выше неё
    # ровно на полугодовую капитализацию
    assert out["cur_yield"] == pytest.approx(15.873, abs=0.001)
    assert out["ytm"] > out["cur_yield"]
    assert out["put_date"] is None          # оферты у бессрочной нет
    assert 0 < out["mod_dur"] < 100 / out["ytm"] * 1.1   # рента: D_mod ≈ 1/y


def test_perpetual_detected_by_flag_or_raw_matdate():
    assert fi._is_perpetual({"perpetual": True, "maturity_date": None})
    assert fi._is_perpetual({"maturity_date": "0000-00-00"})
    assert not fi._is_perpetual({"maturity_date": "2030-01-01"})
    # сырая дата MOEX без флага даёт тот же поток, что и флаг
    raw = fi.compute_fixed_row(_vtb_row(perpetual=False, maturity_date="0000-00-00"),
                               VTB_SUB, None, CD)
    fi.clear_price_metrics_cache()
    flag = fi.compute_fixed_row(_vtb_row(), VTB_SUB, None, CD)
    assert raw["ytm"] == flag["ytm"]


def test_perpetual_passes_universe_filter_without_maturity():
    base = {"isin": "RU000A103SB8", "secid": "RU000A103SB8", "coupon_pct": 10.0,
            "faceunit": "SUR"}
    assert fi._is_fixed({**base, "maturity_date": None, "perpetual": True}, "TQCB", set())
    assert not fi._is_fixed({**base, "maturity_date": None, "perpetual": False}, "TQCB", set())


def test_fetch_board_normalizes_perpetual_matdate(monkeypatch):
    """MATDATE '0000-00-00' → maturity_date None + perpetual: фронт (horizon.js)
    и скринер (years_left) понимают «даты нет» только как None, а строка-нули
    выходила в таблицу «00.00.0000» и вставала первой в сортировке по сроку."""
    payload = {
        "securities": {
            "columns": ["SECID", "ISIN", "SHORTNAME", "MATDATE", "COUPONPERCENT",
                        "FACEVALUE", "FACEVALUEONSETTLEDATE", "FACEUNIT",
                        "ACCRUEDINT", "PREVPRICE", "PREVDATE"],
            "data": [
                ["RU000A103SB8", "RU000A103SB8", "ВТБСУБ1-11", "0000-00-00", 10.0,
                 10_000_000, 10_000_000, "SUR", 5479.45, 63.0, "2026-09-26"],
                ["RU000A0JXFM1", "RU000A0JXFM1", "Дата 01", "2030-01-01", 12.0,
                 1000, 1000, "SUR", 1.0, 99.0, "2026-09-26"],
            ]},
        "marketdata": {"columns": ["SECID", "LAST"], "data": []},
    }

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    async def fake_get(client, url, **kw):
        return _Resp()

    monkeypatch.setattr(fi, "_moex_get", fake_get)
    import asyncio
    rows = {r["isin"]: r for r in asyncio.run(fi._fetch_fixed_board(None, "TQCB"))}
    perp, dated = rows["RU000A103SB8"], rows["RU000A0JXFM1"]
    assert perp["maturity_date"] is None and perp["perpetual"] is True
    assert dated["maturity_date"] == "2030-01-01" and dated["perpetual"] is False


def test_perpetual_undefined_coupons_fill_forward_without_par_redemption():
    """Неопределённый купон бессрочной (ставка после пересмотра не объявлена) —
    не оферта: выкупа номинала на его дату быть не должно, купон берётся
    последним известным."""
    sched = {"coupons": [
        {"start": "2026-06-01", "end": "2026-12-01", "value": 50.0, "face": 1000},
        {"start": "2026-12-01", "end": "2027-06-01", "value": None, "face": 1000},
    ], "amorts": []}
    cfs, face, put = fi.build_fixed_cashflows(sched, CD, 1000.0, perpetual=True)
    assert put is None and face == 1000.0
    assert cfs[:2] == [(date(2026, 12, 1), 50.0), (date(2027, 6, 1), 50.0)]
    assert max(v for _, v in cfs) == 50.0            # номинала в потоке нет
    assert cfs[2][0] == date(2027, 12, 1)            # шаг — полгода, как у графика
    assert cfs[-1][0].year >= CD.year + fi._PERPETUAL_YEARS - 1
    # тот же график как ДАТИРОВАННАЯ бумага с офертой — логика оферты не тронута
    # (без оферт у датированной — продление купона, tests/test_fixed_undefined_coupon.py)
    sched = {**sched, "offers": [{"date": "2026-12-01", "type": "Оферта", "price": 100}]}
    cfs_d, _f, put_d = fi.build_fixed_cashflows(sched, CD, 1000.0)
    assert put_d == date(2026, 12, 1) and cfs_d == [(date(2026, 12, 1), 50.0),
                                                     (date(2026, 12, 1), 1000.0)]


def test_perpetual_flag_does_not_share_cache_with_dated_flow():
    row_dated = _vtb_row(perpetual=False, maturity_date="2027-03-29")
    sched = {**VTB_SUB, "amorts": [{"date": "2027-03-29", "value": 10_000_000}]}
    dated = fi.compute_fixed_row(row_dated, sched, None, CD)
    perp = fi.compute_fixed_row(_vtb_row(), sched, None, CD)
    # к погашению через полгода при цене 63 — около 140 %; рента — ~16 %
    assert dated["ytm"] > 100 and perp["ytm"] < 20


# НорНикЗ26Д (RU000A107C67), 29.09.2026: USD, погашение 27.10.2026.
NORNIK = {
    "coupons": [{"start": "2026-04-27", "end": "2026-10-27", "value": 14.0, "face": 1000}],
    "amorts": [{"date": "2026-10-27", "value": 1000}],
}


def _nornik_row(**kw):
    row = {"isin": "RU000A107C67", "name": "НорНикЗ26Д", "maturity_date": "2026-10-27",
           "faceunit": "USD", "face": 1000.0, "settle_face": 1000.0, "coupon_pct": 2.8,
           # ACCRUEDINT борда TQCB — РУБЛИ (≈ 11.93 USD по курсу ~84)
           "accrued": 1004.44925, "last": 98.9, "prev": 99.86}
    row.update(kw)
    return row


def test_currency_accrued_comes_from_schedule_in_face_ccy():
    out = fi.compute_fixed_row(_nornik_row(), NORNIK, None, CD)
    settle = settle_date(CD)
    acc_usd = 14.0 * (settle - date(2026, 4, 27)).days / (date(2026, 10, 27) - date(2026, 4, 27)).days
    assert out["dirty"] == pytest.approx(989.0 + acc_usd)       # было 1993.45
    # один платёж 1014 через 27 дней — доходность в закрытой форме
    days = (date(2026, 10, 27) - settle).days
    ref = ((1014.0 / out["dirty"]) ** (365.0 / days) - 1) * 100
    assert out["ytm"] == pytest.approx(ref, abs=0.01)
    assert out["ytm"] > 0                                        # было −99.99
    # рублёвый НКД в денежные поля объёма не просачивается
    assert out["accrued_rub"] is None and out["face_value_rub"] is None


def test_ruble_bond_keeps_exchange_accrued():
    """Рублёвая бумага: биржевой ACCRUEDINT — истина, график его не подменяет."""
    row = _nornik_row(faceunit="SUR", accrued=12.34)
    out = fi.compute_fixed_row(row, NORNIK, None, CD)
    assert out["dirty"] == pytest.approx(989.0 + 12.34)


class _Curve:
    def ok(self):
        return True

    def r(self, tau):
        return 0.12


def test_currency_side_metrics_skip_ruble_curve():
    """Стакан/набор тикета (orderbook_svc, universe_stream) зовут
    fixed_side_metrics с КБД напрямую — спред валютной бумаги к рублёвой
    кривой не считаем и там."""
    usd = fi.fixed_side_metrics(_nornik_row(), NORNIK, _Curve(), CD, [98.5])
    assert usd[98.5]["ytm"] is not None and usd[98.5]["g_spread_bps"] is None
    rub = fi.fixed_side_metrics(_nornik_row(faceunit="SUR", accrued=12.34),
                                NORNIK, _Curve(), CD, [98.5])
    assert rub[98.5]["g_spread_bps"] is not None
