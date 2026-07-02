"""
Тесты адаптера НРД Ценового центра.

Проверяют МЕХАНИКУ нормализации (_map_response): что блок собирается из ключей,
заданных в NRD_FIELD_MAP / NRD_RATING_MAP / NRD_LIQUIDITY_MAP. Когда придёт реальный
пример JSON из кабинета НРД — обновить значения мапов и SAMPLE_* ниже, тест станет
контрактным.
"""
import asyncio
from services import nrd


# Реальные ключи valuationnewadd + подтверждённые на живых данных масштабы:
#   yields/z/g/nominal — доли (×100 / ×10000); simple/discount — проценты (×100).
SAMPLE_NEW = {}  # valuationnew у подписки нет доступа (403) -> fair_value None
SAMPLE_ADD = {
    "isin": "RU000A10BF48",
    "val_date": "2026-06-29",
    "wa_price": "100.83",
    "legal_close_price": "100.83",
    "duration": "0.082192",
    "mod_duration": "0.082192",
    "convexity": "0.003245",
    "pvbp": "0.000834",
    "current_yield": "0.159062",   # доля -> 15.9062 %
    "yield_maturity": "0.170911",  # -> 17.0911 %
    "ytm_close": "0.170658",
    "z_spread": "0.038",           # доля -> 380 bps
    "g_spread": "0.038044",
    "nominal_margin": "0.017",     # доля -> 170 bps (= спред выпуска)
    "simple_margin": "0,670268",   # процент -> 67 bps (запятая)
    "discount_margin": "0.345359",  # процент -> 35 bps
    "base_coupon_index": "CBRATED",
    "coupon_type": "float",
    "akra_rating": "AAA(RU)",
    "expert_rating": "RUAAA",
    "trade_volume_rub": "0",
    "trade_volume_qty": "16568",
}


def test_map_response_scales():
    out = nrd._map_response(SAMPLE_NEW, SAMPLE_ADD)
    assert out["fair_value_pct"] is None          # нет valuationnew
    assert out["nrd_price_pct"] == 100.83
    assert out["duration"] == 0.082192
    assert out["current_yield_pct"] == 15.9062    # доля ×100
    assert out["ytm_pct"] == 17.0911
    assert out["z_spread_bps"] == 380             # доля ×10000
    assert out["nominal_margin_bps"] == 170       # = спред выпуска Яндекса
    assert out["simple_margin_bps"] == 67         # процент ×100
    assert out["discount_margin_bps"] == 35
    assert out["base_coupon_index"] == "CBRATED"
    assert out["coupon_type"] == "float"
    assert out["nrd_calc_date"] == "2026-06-29"


def test_map_response_ratings_and_liquidity():
    out = nrd._map_response(SAMPLE_NEW, SAMPLE_ADD)
    assert out["ratings"] == {"akra": "AAA(RU)", "expert_ra": "RUAAA"}
    assert out["liquidity"]["volume_qty"] == 16568.0


def test_map_response_missing_fields_are_none():
    out = nrd._map_response({}, {})
    assert out["fair_value_pct"] is None
    assert out["z_spread_bps"] is None
    assert out["ratings"] is None
    assert out["liquidity"] is None


def test_fetch_nrd_metrics_graceful_when_unconfigured(monkeypatch):
    # без NRD_LOGIN/NRD_APIKEY — пустой результат, без исключений
    monkeypatch.setattr(nrd, "NRD_LOGIN", "")
    monkeypatch.setattr(nrd, "NRD_APIKEY", "")
    result = asyncio.run(nrd.fetch_nrd_metrics(["RU000A10BF48"]))
    assert result == {}


def test_num_helper_handles_messy_strings():
    assert nrd._num("1 234,56") == 1234.56
    assert nrd._num("") is None
    assert nrd._num(None) is None
    assert nrd._num("12.5") == 12.5
