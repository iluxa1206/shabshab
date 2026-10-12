"""Y-IDX у ФИКСОВ — та же база, что у флоатеров (docs/unified_monitor_tz.md).

Флоатер меряется спредом к роллированию RUONIA (Y-IDX), фикс — g-спредом к КБД.
Это две разные базы: между ними базис ОФЗ–OIS, зависящий от срока, и в общей
витрине сортировка смеси по одной колонке сравнивала бы разные вещи. Поэтому
фиксу считаем Y-IDX той же формулой: YTM − доходность роллирования RUONIA до
КОНЦА ПОТОКА (у бумаги с пут-офертой — до выкупа, как у флоатера, обрезанного
офертой).
"""
from datetime import date

import pytest

from core.forwards import CurveBootstrapper
from core.rates import Quote
from core.valuation import ruonia_rolling_yield_pct, settle_date
from services import fixed_income as fi
from services import universe_stream as us
from services.custom_bond import build_custom_schedule, _accrued

CD = date(2026, 9, 29)   # вторник → поставка 30.09
MAT = date(2029, 9, 15)


def _flat_ruonia(level_pct: float):
    tenors = ["1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y"]
    return CurveBootstrapper.bootstrap_ruonia(
        [Quote("SYN", t, level_pct, CD) for t in tenors], CD)


@pytest.fixture(scope="module")
def ru():
    return _flat_ruonia(15.0)


@pytest.fixture
def sched():
    return build_custom_schedule(MAT, 14.5, 4, 1000.0, CD)


@pytest.fixture(autouse=True)
def no_live_ticks(monkeypatch):
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: None)
    fi.clear_price_metrics_cache()


def _row(sched, **kw):
    row = {"isin": "RU000A1YIDX1", "faceunit": "SUR", "face": 1000.0, "settle_face": 1000.0,
           "last": 98.0, "prev": 98.0, "accrued": _accrued(sched, settle_date(CD), CD)}
    row.update(kw)
    return row


def test_yidx_is_ytm_minus_ruonia_rolling_to_maturity(sched, ru):
    out = fi.compute_fixed_row(_row(sched), sched, None, CD, ru_curve=ru)
    base = ruonia_rolling_yield_pct(ru, CD, MAT)
    assert out["index_yield_pct"] == pytest.approx(base, abs=1e-4)
    # YTM в строке округлён до сотых — сверка с точностью до округления
    assert out["yield_over_index_bps"] == pytest.approx((out["ytm"] - base) * 100, abs=1)


def test_no_ruonia_curve_no_yidx_and_nothing_else_changes(sched, ru):
    """Кривую не передали — Y-IDX честным прочерком; остальное не зависит от неё."""
    bare = fi.compute_fixed_row(_row(sched), sched, None, CD)
    fi.clear_price_metrics_cache()
    full = fi.compute_fixed_row(_row(sched), sched, None, CD, ru_curve=ru)
    assert bare["yield_over_index_bps"] is None and bare["index_yield_pct"] is None
    for k in ("ytm", "mod_dur", "dirty", "ytm_bid", "ytm_ask"):
        assert bare.get(k) == full.get(k)


def test_sides_and_wap_get_own_yidx(sched, ru):
    """База от цены не зависит: Y-IDX стороны — YTM стороны минус та же база,
    дешевле бида выше спред."""
    out = fi.compute_fixed_row(_row(sched, bid=97.5, ask=98.5, wap=98.2), sched, None, CD,
                               ru_curve=ru)
    base = out["index_yield_pct"]
    for side in ("bid", "ask", "wap"):
        assert out[f"y_idx_{side}_bps"] == pytest.approx((out[f"ytm_{side}"] - base) * 100, abs=1)
    assert out["y_idx_bid_bps"] > out["yield_over_index_bps"] > out["y_idx_ask_bps"]


def test_put_offer_moves_base_horizon_to_buyback(ru):
    """Поток режется выкупом по оферте (купон после неё не объявлен) — база
    обязана считаться до выкупа, а не до погашения через семь лет: иначе
    доходность к оферте делилась бы на роллирование до 2033 (та же ловушка,
    что у флоатеров, services/valuation: Россети1Р8 56 bps против 152)."""
    gpb = {
        "coupons": [
            {"start": "2026-04-18", "end": "2026-10-18", "value": 50.14, "valueprc": 10.0, "face": 1000},
            {"start": "2026-10-18", "end": "2027-04-18", "value": None, "valueprc": None, "face": 1000},
        ],
        "amorts": [{"date": "2033-04-18", "value": 1000}],
        "offers": [{"date": "0000-00-00", "type": "Оферта/Погашение", "price": None},
                   {"date": "2026-10-21", "type": "Оферта", "price": 100}],
    }
    row = {"isin": "RU000A1064G3", "faceunit": "SUR", "face": 1000.0, "settle_face": 1000.0,
           "accrued": 45.21, "last": 99.68, "prev": 99.68}
    out = fi.compute_fixed_row(row, gpb, None, CD, ru_curve=ru)
    assert out["put_date"] == "2026-10-21"
    assert out["index_yield_pct"] == pytest.approx(
        ruonia_rolling_yield_pct(ru, CD, date(2026, 10, 21)), abs=1e-4)


@pytest.mark.parametrize("kw", [
    {"faceunit": "USD"},          # RUONIA рублёвая — тот же guard, что у КБД
    {"perpetual": True},          # горизонта нет — рента на 100 лет
    {"linked": True},             # YTM реальная, RUONIA номинальная
])
def test_no_yidx_where_ruonia_base_is_meaningless(sched, ru, kw, monkeypatch):
    # НКД валютной бумаги считается из дат купонов, а синтетика их не несёт —
    # без подмены строка ушла бы в прочерк раньше и guard бы не проверялся
    monkeypatch.setattr(fi, "_accrued_face_ccy", lambda row, full, cd: row["accrued"])
    out = fi.compute_fixed_row(_row(sched, **kw), sched, None, CD, ru_curve=ru)
    assert out["ytm"] is not None
    assert out["yield_over_index_bps"] is None
    assert out["y_idx_bid_bps"] is None and out["y_idx_wap_bps"] is None


def test_side_metrics_return_yidx(sched, ru):
    """Стакан и набор тикета идут через fixed_side_metrics — Y-IDX уровня там же."""
    got = fi.fixed_side_metrics(_row(sched), sched, None, CD, [97.0, 99.0], ru_curve=ru)
    assert got[97.0]["y_idx_bps"] > got[99.0]["y_idx_bps"]


def test_engine_row_and_patch_carry_yidx(sched, ru):
    """Движок передаёт RUONIA-кривую контекста в расчёт фикса, и Y-IDX уезжает
    WS-патчем под теми же именами, что у флоатеров."""
    row = _row(sched, isin="RU000A1YIDX2", secid="YIDX2", cls="corp")
    ctx = {"uni_by": {}, "fixed_by": {row["isin"]: row}, "cache": {}, "secs": {},
           "board": {}, "ruonia_curve": ru, "keyrate_curve": None,
           "exp_ks": None, "exp_ru": None, "g_curve": None,
           "calc_date": CD, "version": (CD.isoformat(), 1.0), "full_by": {row["isin"]: sched}}
    us._fixed_isins.clear()
    got = us._crunch([(row["isin"], {"last_price": 98.0, "bid": 97.5, "ask": 98.5})], ctx)
    r = got[row["isin"]]
    assert r["yield_over_index_bps"] is not None and r["y_idx_bid_bps"] is not None
    patch = us._fixed_patch(r)
    for k in ("yield_over_index_bps", "y_idx_bid_bps", "y_idx_ask_bps", "index_yield_pct"):
        assert k in patch
