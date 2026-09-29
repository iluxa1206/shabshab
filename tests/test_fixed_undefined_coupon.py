"""ФИКСЫ: неопределённый будущий купон у датированной бумаги.

Раньше любой купон без суммы в bondization считался офертой: поток рвался на
дате последнего ИЗВЕСТНОГО купона выкупом остатка номинала по 100. Снимок прода
29.09.2026 — у 38 из 157 таких бумаг будущей оферты в графике MOEX нет, и выкуп
по номиналу через несколько дней давал мусор: РКСОлмA902 189 % (MOEX 16.35),
Сбер2СУБ3R 57.8 % (10.06), АЛЬФАЗО350 225 % (7.51). А у бумаг с офертой выкуп
стоял на дате купона, хотя MOEX выкупает на 2–14 дней позже: ГПБ001P26P 17.07
против YIELDTOOFFER 14.57.

Теперь (services/fixed_income.build_fixed_cashflows):
* ставка объявлена, сумма нет (ABS/ИЦБ) — сумма по ставке и номиналу в обращении;
* ставка не объявлена — последний известный купон продлевается до будущей
  оферты MOEX (выкуп на ДАТЕ ВЫКУПА MOEX, а не на дате купона), а без неё — до
  погашения (как MOEX).

Эталоны — числа MOEX того же дня (YIELD / YIELDATPREVWAPRICE / YIELDTOOFFER)
при той же цене и НКД: независимая сверка, а не наш же код.
"""
from datetime import date, timedelta

import pytest

from services import fixed_income as fi
from services.market_data import CALL_OFFER_SOURCE


CD = date(2026, 9, 29)   # вторник → поставка 30.09


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: None)
    fi.clear_price_metrics_cache()


def _cp(start, end, value=None, rate=None, face=1000):
    return {"start": start, "end": end, "value": value, "valueprc": rate, "face": face}


def _row(isin, px, accrued, face=1000.0, faceunit="SUR", **kw):
    row = {"isin": isin, "faceunit": faceunit, "face": face, "settle_face": face,
           "accrued": accrued, "last": px, "prev": px}
    row.update(kw)
    return row


# Сбер2СУБ3R (RU000A103Q24): субординированный, 8.5 % до 20.11.2026, дальше
# ставка не объявлена; оферт у MOEX нет (только '0000-00-00'), погашение 2031.
_SBER_DATES = ["2026-11-20", "2027-05-21", "2027-11-19", "2028-05-19", "2028-11-17",
               "2029-05-18", "2029-11-16", "2030-05-17", "2030-11-15", "2031-05-16",
               "2031-11-14"]
SBER_SUB = {
    "coupons": [
        _cp("2025-11-21", "2026-05-22", 423835.62, 8.5, 10_000_000),
        _cp("2026-05-22", "2026-11-20", 423835.62, 8.5, 10_000_000),
        *(_cp(s, e, face=10_000_000) for s, e in zip(_SBER_DATES, _SBER_DATES[1:])),
    ],
    "amorts": [{"date": "2031-11-14", "value": 10_000_000}],
    "offers": [{"date": "0000-00-00", "type": "Оферта/Погашение", "price": None}],
}


def test_no_offer_extends_last_coupon_to_maturity_like_moex():
    # цена — PREVWAPRICE 94.75, НКД — биржевой ACCRUEDINT
    out = fi.compute_fixed_row(_row("RU000A103Q24", 94.75, 305068.49, face=10_000_000.0),
                               SBER_SUB, None, CD)
    assert out["ytm"] == pytest.approx(10.06, abs=0.01)   # MOEX 10.06; было 57.81
    assert out["put_date"] is None                        # оферты нет — горизонт погашение
    assert out["coupon_assumed_from"] == "2026-11-20"     # число держится на допущении
    assert out["mod_dur"] > 3                              # было 0.09: «выкуп» через 51 день


def test_extended_coupon_is_last_known_value_for_equal_periods():
    cfs, face, put, assumed = fi.build_fixed_cashflows(SBER_SUB, CD, 10_000_000,
                                                       with_assumed=True)
    assert put is None and assumed == date(2026, 11, 20) and face == 10_000_000
    coupons = [v for d, v in cfs if d < date(2031, 11, 14)]
    # периоды по 182 дня — буквально та же сумма, что у последнего известного
    assert coupons == pytest.approx([423835.62] * len(coupons))
    assert len(coupons) == 10
    # в дату погашения — продлённый купон и номинал
    assert sorted(v for d, v in cfs if d == date(2031, 11, 14)) == \
        pytest.approx([423835.62, 10_000_000])
    # публичный API без флага — прежний кортеж из трёх
    assert len(fi.build_fixed_cashflows(SBER_SUB, CD, 10_000_000)) == 3


# АЛЬФАЗО350 (RU000A108JQ4): USD, 5.5 % полугодовой до 26.10.2026, дальше не
# объявлен, оферт нет, погашение 2031. НКД — из графика в долларах (TQCB
# ACCRUEDINT 1986.11 — рубли).
_ALFA_DATES = [f"{y}-{m}-26" for y in range(2026, 2032) for m in ("04", "10")][1:]
ALFA_USD = {
    "coupons": [
        _cp("2025-10-26", "2026-04-26", 27.7643731093, 5.5),
        _cp("2026-04-26", "2026-10-26", 27.5, 5.5),
        *(_cp(s, e) for s, e in zip(_ALFA_DATES, _ALFA_DATES[1:])),
    ],
    "amorts": [{"date": "2031-10-26", "value": 1000}],
    "offers": [{"date": "0000-00-00", "type": "Оферта/Погашение", "price": None}],
}


def test_currency_bond_without_offer_matches_moex():
    out = fi.compute_fixed_row(_row("RU000A108JQ4", 92.1127, 1986.108475, faceunit="USD"),
                               ALFA_USD, None, CD)
    assert out["ytm"] == pytest.approx(7.51, abs=0.01)    # MOEX 7.51; было 225.17
    assert out["put_date"] is None and out["coupon_assumed_from"] == "2026-10-26"


# ГПБ001P26P (RU000A1064G3): купон до 18.10.2026, оферта MOEX — выкуп 21.10.
GPB = {
    "coupons": [
        _cp("2026-04-18", "2026-10-18", 50.14, 10.0),
        _cp("2026-10-18", "2027-04-18"),
        _cp("2027-04-18", "2027-10-18"),
    ],
    "amorts": [{"date": "2033-04-18", "value": 1000}],
    "offers": [{"date": "0000-00-00", "type": "Оферта/Погашение", "price": None},
               {"date": "2026-10-21", "type": "Оферта", "price": 100}],
}


def test_offer_redeems_on_moex_buyback_date_not_coupon_date():
    out = fi.compute_fixed_row(_row("RU000A1064G3", 99.68, 45.21), GPB, None, CD)
    assert out["put_date"] == "2026-10-21"                # было 2026-10-18 — дата купона
    assert out["ytm"] == pytest.approx(14.57, abs=0.01)   # MOEX YIELDTOOFFER 14.5696; было 17.07
    assert out["coupon_assumed_from"] is None             # к оферте — ничего не предполагаем
    cfs, _face, put = fi.build_fixed_cashflows(GPB, CD, 1000.0)
    assert cfs == [(date(2026, 10, 18), 50.14), (date(2026, 10, 21), 1000.0)]


# Роделен2P1 (RU000A107076): оферта 01.10.2026 уже с новой ставкой 23.5 %,
# купоны объявлены до 21.07.2027, дальше — нет; погашение 13.09.2028.
_RODELEN_DATES = [date(2027, 7, 21) + timedelta(days=30 * k) for k in range(15)]
RODELEN = {
    "coupons": [
        _cp("2026-08-25", "2026-09-24", 18.49, 22.5),
        *(_cp(s, e, 19.32, 23.5) for s, e in [
            ("2026-09-24", "2026-10-24"), ("2026-10-24", "2026-11-23"),
            ("2026-11-23", "2026-12-23"), ("2026-12-23", "2027-01-22"),
            ("2027-01-22", "2027-02-21"), ("2027-02-21", "2027-03-23"),
            ("2027-03-23", "2027-04-22"), ("2027-04-22", "2027-05-22"),
            ("2027-05-22", "2027-06-21"), ("2027-06-21", "2027-07-21")]),
        *(_cp(s.isoformat(), e.isoformat())
          for s, e in zip(_RODELEN_DATES, _RODELEN_DATES[1:])),
    ],
    "amorts": [{"date": "2028-09-13", "value": 1000}],
    "offers": [{"date": "2026-01-14", "type": "Оферта (состоялось)", "price": 100},
               {"date": "2026-10-01", "type": "Оферта", "price": 100}],
}


def test_offer_before_boundary_is_past_rate_reset_not_horizon():
    """Оферта 01.10 — не граница: после неё ставка уже объявлена на 10 месяцев.
    Будущей оферты на границе 21.07.2027 MOEX не вывесил — горизонт погашение,
    купон продлён. (Раньше — выкуп 21.07.2027, 34.97 %: через два дня, когда
    оферта 01.10 станет прошлой, правило «есть история оферт» перещёлкнулось
    бы на другой горизонт — у бумаги без событий скакал бы YTM.)"""
    out = fi.compute_fixed_row(_row("RU000A107076", 95.15, 3.86), RODELEN, None, CD)
    assert out["put_date"] is None
    assert out["coupon_assumed_from"] == "2027-07-21"
    assert 29 < out["ytm"] < 32                           # было 34.97 к «оферте»
    cfs, _f, _p = fi.build_fixed_cashflows(RODELEN, CD, 1000.0)
    tail = [v for d, v in cfs if d > date(2027, 7, 21)]
    assert tail[:-1] == pytest.approx([19.32] * 14)       # периоды по 30 дней — та же сумма
    assert tail[-1] == 1000.0


# iХРОМОС Б1 (RU000A108405): оферты у выпуска только ПРОШЛЫЕ (01.04.2025,
# 23.09.2026), после последней ставка 10 % объявлена на 2 месяца; дальше не
# объявлена, номинал гасится траншами до 07.03.2028.
_CHROMOS_DATES = ["2026-11-13", "2026-12-13", "2027-01-12", "2027-02-11", "2027-03-13",
                  "2027-04-12", "2027-05-12", "2027-06-11", "2027-07-11", "2027-08-10",
                  "2027-09-09", "2027-10-09", "2027-11-08", "2027-12-08", "2028-01-07",
                  "2028-02-06", "2028-03-07"]
CHROMOS = {
    "coupons": [
        _cp("2026-08-15", "2026-09-14", 24.25, 29.5),
        _cp("2026-09-14", "2026-10-14", 8.22, 10.0),
        _cp("2026-10-14", "2026-11-13", 8.22, 10.0),
        *(_cp(s, e) for s, e in zip(_CHROMOS_DATES, _CHROMOS_DATES[1:])),
    ],
    "amorts": [{"date": "2027-03-13", "value": 100}, {"date": "2027-06-11", "value": 100},
               {"date": "2027-09-09", "value": 100}, {"date": "2027-12-08", "value": 100},
               {"date": "2028-03-07", "value": 600}],
    "offers": [{"date": "2025-04-01", "type": "Оферта", "price": 100},
               {"date": "2026-09-23", "type": "Оферта", "price": 100}],
}


def test_past_offers_only_extend_like_moex_on_amortizing_face():
    out = fi.compute_fixed_row(_row("RU000A108405", 97.6, 4.38), CHROMOS, None, CD)
    assert out["ytm"] == pytest.approx(12.90, abs=0.01)   # MOEX 12.90; было 35.16
    assert out["put_date"] is None and out["coupon_assumed_from"] == "2026-11-13"
    by_date = dict(fi.build_fixed_cashflows(CHROMOS, CD, 1000.0)[0])
    # купон продлён на номинал в обращении: после транша 13.03.2027 — 900 из 1000
    assert by_date[date(2027, 2, 11)] == pytest.approx(8.22)
    assert by_date[date(2027, 4, 12)] == pytest.approx(8.22 * 0.9)


def test_offer_after_boundary_extends_coupon_up_to_it():
    """Оферта вывешена позже границы: купоны между ними ставкой не объявлены —
    продлеваем последний известный до оферты, на ней выкуп остатка."""
    sched = {**SBER_SUB, "offers": [{"date": "2027-11-22", "type": "Оферта", "price": 100}]}
    cfs, _f, put, assumed = fi.build_fixed_cashflows(sched, CD, 10_000_000,
                                                     with_assumed=True)
    assert put == date(2027, 11, 22) and assumed == date(2026, 11, 20)
    assert cfs == pytest.approx([(date(2026, 11, 20), 423835.62),
                                 (date(2027, 5, 21), 423835.62),
                                 (date(2027, 11, 19), 423835.62),
                                 (date(2027, 11, 22), 10_000_000.0)])


# СовкВзск01 (RU000A10EMM0): ставка 17 % объявлена на весь срок, но MOEX
# посчитал суммы только до 25.04.2028 — дальше value пусто, valueprc есть.
_SOVK_DATES = ["2028-04-25", "2028-07-25", "2028-10-25", "2029-01-25", "2029-04-25",
               "2029-07-25", "2029-10-25", "2030-01-25", "2030-04-25"]
SOVK = {
    "coupons": [
        _cp("2026-07-25", "2026-10-25", 42.85, 17.0),
        _cp("2026-10-25", "2027-01-25", 42.85, 17.0),
        _cp("2027-01-25", "2027-04-25", 41.92, 17.0),
        _cp("2027-04-25", "2027-07-25", 42.38, 17.0),
        _cp("2027-07-25", "2027-10-25", 42.85, 17.0),
        _cp("2027-10-25", "2028-01-25", 42.85, 17.0),
        _cp("2028-01-25", "2028-04-25", 42.38, 17.0),
        *(_cp(s, e, None, 17.0) for s, e in zip(_SOVK_DATES, _SOVK_DATES[1:])),
    ],
    "amorts": [{"date": "2030-04-25", "value": 1000}],
    "offers": [{"date": "0000-00-00", "type": "Оферта/Погашение", "price": None}],
}


def test_declared_rate_without_amount_is_priced_by_rate_not_as_offer():
    out = fi.compute_fixed_row(_row("RU000A10EMM0", 100.14, 31.21), SOVK, None, CD)
    assert out["ytm"] == pytest.approx(18.05, abs=0.01)   # MOEX 18.05; было 17.98 к «оферте»
    assert out["put_date"] is None
    assert out["coupon_assumed_from"] is None             # ставка объявлена — не допущение
    cfs, _f, _p = fi.build_fixed_cashflows(SOVK, CD, 1000.0)
    by_date = dict(cfs)
    # 1000 × 17 % × 91/365 — та же конвенция, что у объявленных сумм (42.38)
    assert by_date[date(2028, 7, 25)] == pytest.approx(1000 * 0.17 * 91 / 365)


def test_declared_rate_follows_amortizing_face_and_stops_after_full_redemption():
    """ABS: сумма купона зависит от будущей амортизации пула — считаем от
    номинала в обращении; после полного погашения по графику купонов нет."""
    sched = {
        "coupons": [
            _cp("2026-09-01", "2026-10-01", 10.0, 12.0),
            _cp("2026-10-01", "2026-11-01", None, 12.0),
            _cp("2026-11-01", "2026-12-01", None, 12.0),
            _cp("2026-12-01", "2027-01-01", None, 12.0),
        ],
        "amorts": [{"date": "2026-10-01", "value": 400.0},
                   {"date": "2026-11-01", "value": 600.0}],
        "offers": [],
    }
    cfs, face, put = fi.build_fixed_cashflows(sched, CD, 1000.0)
    assert put is None and face == 1000.0
    by_date = {}
    for d, v in cfs:
        by_date[d] = by_date.get(d, 0.0) + v
    # ноябрьский купон — на 600 в обращении (транш 01.10 уже погашен)
    assert by_date[date(2026, 11, 1)] == pytest.approx(600 * 0.12 * 31 / 365 + 600)
    assert date(2026, 12, 1) not in by_date and date(2027, 1, 1) not in by_date


def _call(d):
    """Синтетическая запись колла из реестра — как её кладёт
    market_data._with_call_offers поверх ответа MOEX."""
    return {"date": d, "type": "call-опцион", "price": None, "source": CALL_OFFER_SOURCE}


def test_registry_call_is_not_a_horizon_for_fixed():
    """Прод 29.09.2026: у АЛЬФАЗО350 колл реестра ровно на границе (26.10), цена
    92 — рынок отзыва не ждёт. Прими мы колл за оферту, остались бы те же 225 %.
    Колл — опцион эмитента, от цены зависит (правило горизонта), а шаблон потока
    от цены не зависит: в поток фиксов колл не берём ни на границе, ни как
    историю оферт — как и MOEX в YIELD."""
    alfa = {**ALFA_USD, "offers": ALFA_USD["offers"] + [_call("2026-10-26")]}
    out = fi.compute_fixed_row(_row("RU000A108JQ4", 92.1127, 1986.108475, faceunit="USD"),
                               alfa, None, CD)
    assert out["put_date"] is None
    assert out["ytm"] == pytest.approx(7.51, abs=0.01)


def test_moex_offer_wins_over_registry_call_on_same_boundary():
    """Филберт01 / ГПБ001P19P: колл реестра стоит на дате купона, выкуп MOEX —
    на 5–9 дней позже. Горизонт — дата выкупа MOEX."""
    gpb = {**GPB, "offers": GPB["offers"] + [_call("2026-10-18")]}
    cfs, _f, put = fi.build_fixed_cashflows(gpb, CD, 1000.0)
    assert put == date(2026, 10, 21)


def test_nothing_to_extend_hides_ytm():
    """Оферт нет и продлевать нечего — прочерк, а не выдуманный поток:
    ни одного известного купона или нет графика погашения (поток без номинала)."""
    no_known = {"coupons": [_cp("2026-09-01", "2026-12-01"), _cp("2026-12-01", "2027-03-01")],
                "amorts": [{"date": "2027-03-01", "value": 1000}], "offers": []}
    out = fi.compute_fixed_row(_row("XS0000000001", 95.0, 0.0), no_known, None, CD)
    assert out["ytm"] is None
    no_amort = {"coupons": [_cp("2026-06-01", "2026-12-01", 50.0, 10.0),
                            _cp("2026-12-01", "2027-06-01")],
                "amorts": [], "offers": []}
    fi.clear_price_metrics_cache()
    out = fi.compute_fixed_row(_row("XS0000000002", 95.0, 0.0), no_amort, None, CD)
    assert out["ytm"] is None


def test_offer_change_is_not_served_from_stale_cache():
    """Оферта решает горизонт — значит входит в ключ кэша потока и метрик:
    MOEX вывесил оферту (или реестр добавил колл) при тех же купонах."""
    row = _row("RU000A103Q24", 94.75, 305068.49, face=10_000_000.0)
    before = fi.compute_fixed_row(row, SBER_SUB, None, CD)
    with_offer = {**SBER_SUB, "offers": [{"date": "2026-11-24", "type": "Оферта",
                                          "price": 100}]}
    after = fi.compute_fixed_row(row, with_offer, None, CD)
    assert before["put_date"] is None and after["put_date"] == "2026-11-24"
    assert after["ytm"] > 40 > before["ytm"]
