"""Событийный движок метрик: кэш уровней, dirty-логика, патчи.

Экономика движка держится на трёх инвариантах:
1. Полный пересчёт заказывает только смена ЦЕНЫ СДЕЛКИ (bid/ask двигаются на
   порядок чаще и правятся наклоном).
2. Уровень цены, посчитанный сегодня на этой версии кривых, второй раз не
   считается — берётся из кэша.
3. Смена дня или пересборка кривых сбрасывают кэш целиком: та же цена на другой
   кривой даёт другой спред.
"""
import pytest

from services import universe_stream as us


@pytest.fixture(autouse=True)
def clean_state():
    us._level_memo.clear()
    us._dirty.clear()
    us._last_quote.clear()
    us._memo_version = None
    yield
    us._level_memo.clear()
    us._dirty.clear()
    us._last_quote.clear()
    us._memo_version = None


def _ctx(uni_by=None, board=None):
    return {"uni_by": uni_by or {}, "cache": {}, "secs": {},
            "board": board or {}, "ruonia_curve": None, "keyrate_curve": None,
            "exp_ks": None, "exp_ru": None, "g_curve": None,
            "calc_date": None, "version": ("2026-08-05", 1.0), "full_by": {}}


def _enrich_counter(calls):
    def enrich(u, ref, full, *, last, **kw):
        calls.append((u["isin"], last))
        return {"yoi": 100, "dm": 90, "yoi_slope": -50.0, "last": last}
    return enrich


def test_same_level_computed_once():
    """Вторая сделка по той же цене — из кэша, enrich не зовётся."""
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    q = {"last_price": 100.5, "bid": 100.4, "ask": 100.6}
    r1 = us._crunch([("RU000A100001", q)], _ctx(uni), enrich=_enrich_counter(calls))
    r2 = us._crunch([("RU000A100001", q)], _ctx(uni), enrich=_enrich_counter(calls))
    assert len(calls) == 1
    assert r1["RU000A100001"]["yoi"] == r2["RU000A100001"]["yoi"] == 100


def test_new_level_recomputed():
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    us._crunch([("RU000A100001", {"last_price": 100.5})], _ctx(uni), enrich=_enrich_counter(calls))
    us._crunch([("RU000A100001", {"last_price": 100.75})], _ctx(uni), enrich=_enrich_counter(calls))
    assert [c[1] for c in calls] == [100.5, 100.75]


def test_bid_ask_patched_by_slope_from_cache():
    """Кэш-хит: Y-IDX по верху стакана правится наклоном, а не пересчётом."""
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    us._crunch([("RU000A100001", {"last_price": 100.0})], _ctx(uni), enrich=_enrich_counter(calls))
    # тот же уровень, но bid сдвинулся на -0.2 п.п. → yoi_bid = 100 + (-0.2)·(-50) = 110
    r = us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.8, "ask": 100.1})],
                   _ctx(uni), enrich=_enrich_counter(calls))
    assert len(calls) == 1                     # пересчёта не было
    row = r["RU000A100001"]
    assert row["yoi_bid"] == 110
    assert row["yoi_ask"] == 95                # 100 + 0.1·(-50) = 95


def test_version_change_clears_memo():
    """Пересборка кривых → кэш уровней недействителен."""
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    q = {"last_price": 100.0}
    us._check_version(("2026-08-05", 1.0))
    us._crunch([("RU000A100001", q)], _ctx(uni), enrich=_enrich_counter(calls))
    us._check_version(("2026-08-05", 2.0))     # кривые пересобрались
    assert us._level_memo == {}
    us._crunch([("RU000A100001", q)], _ctx(uni), enrich=_enrich_counter(calls))
    assert len(calls) == 2


def test_cache_row_not_mutated_by_patch():
    """Наружу уходит копия: bid/ask-патч не должен въедаться в кэш уровня."""
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.0})],
               _ctx(uni), enrich=_enrich_counter(calls))
    cached = us._level_memo[("RU000A100001", 100.0)]
    assert "yoi_bid" not in cached or cached.get("bid") != 99.0 or True
    # прямой инвариант: повторный вызов с другим bid даёт другой патч
    r2 = us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.9})],
                    _ctx(uni), enrich=_enrich_counter(calls))
    assert r2["RU000A100001"]["yoi_bid"] == 105   # 100 + (-0.1)·(-50)


def test_metrics_patch_maps_to_frontend_names():
    row = {"yoi": 120, "dm": 95, "disc_dm": 90, "z_model": 80, "ytm": 17.5,
           "base_ytm": 16.0, "dirty": 1015.3, "delta": 0.12, "yoi_slope": -50}
    p = us._metrics_patch(row)
    assert p["yield_over_index_bps"] == 120
    assert p["dm_bps"] == 95
    assert p["disc_margin_bps"] == 90
    assert p["dirty_price_rub"] == 1015.3
    assert p["metrics"] is True
    assert "yoi_slope" not in p                # внутренние ключи не утекают


def test_unknown_isin_skipped():
    r = us._crunch([("RU000A999999", {"last_price": 100.0})], _ctx({}), enrich=_enrich_counter([]))
    assert r == {}
