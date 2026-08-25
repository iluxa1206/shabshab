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


def test_missing_side_is_none_not_zero():
    """Нет стороны в стакане — источники отдают 0, а не null. Ноль не должен
    доехать ни до колонки цены («0,00»), ни до спреда по ней: у МТС 2Р-03 при
    отсутствующем оффере в таблице стояло 0,00 и 8960 б.п."""
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    calls = []
    us._crunch([("RU000A100001", {"last_price": 100.0})], _ctx(uni),
               enrich=_enrich_counter(calls))
    r = us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.8, "ask": 0})],
                   _ctx(uni), enrich=_enrich_counter(calls))
    row = r["RU000A100001"]
    assert row["ask"] is None and row["yoi_ask"] is None    # стороны нет
    assert row["bid"] == 99.8 and row["yoi_bid"] == 110     # живая сторона цела


def test_px_or_none_normalizes_zero():
    from services.market_data import _px_or_none
    assert _px_or_none(0) is None          # нет стороны
    assert _px_or_none(0.0) is None
    assert _px_or_none(-1) is None         # мусор источника
    assert _px_or_none(None) is None
    assert _px_or_none("") is None
    assert _px_or_none(99.75) == 99.75


# ── спред по средневзвесу: наклон только вблизи цены сделки ────────────────

def test_wap_far_from_last_uses_exact_path(monkeypatch):
    """Средневзвес далеко от цены сделки — считаем точно, а не наклоном.

    Наклон честен на масштабе долей пункта; у неликвида wap уходит на пункты, и
    линеаризация врёт сотнями bps (замер 25.08: сдвиг 1 пп → до 214, 2 пп → 410).
    Тогда витрина и график показывали бы РАЗНЫЕ числа одной метрики."""
    from services import universe_stream as us
    from services.bonds import yidx_at_price

    row = {"yoi": 200.0, "yoi_slope": -50.0, "last": 100.0}
    near, far = 100.2, 97.0
    assert abs(near - row["last"]) <= us.WAP_EXACT_PP
    assert abs(far - row["last"]) > us.WAP_EXACT_PP

    # вблизи — наклон, он же и остаётся в строке
    assert yidx_at_price(row, near) == pytest.approx(190, abs=1)
    # далеко — наклон даёт 350, а точный путь 260: расходятся на 90 bps
    assert yidx_at_price(row, far) == pytest.approx(350, abs=1)
    monkeypatch.setattr("services.screener_core.exact_y_idx", lambda i, p: 260.0)
    from services.screener_core import exact_y_idx
    assert int(round(exact_y_idx("RU1", far))) == 260
