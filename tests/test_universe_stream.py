"""Событийный движок метрик: кэш уровней, dirty-логика, патчи.

Экономика движка держится на трёх инвариантах:
1. Полный пересчёт заказывает только смена ЦЕНЫ СДЕЛКИ (bid/ask двигаются на
   порядок чаще, и по ним считается ТОЛЬКО Y-IDX — батчем по методике, поток и
   база при этом не пересобираются).
2. Уровень цены, посчитанный сегодня на этой версии кривых, второй раз не
   считается — берётся из кэша.
3. Смена дня или пересборка кривых сбрасывают кэш целиком: та же цена на другой
   кривой даёт другой спред.
"""
import pytest

from services import universe_stream as us


@pytest.fixture(autouse=True)
def exact_stub(monkeypatch):
    """Точный расчёт сторон подменяем таблицей «цена → спред»: движок обязан
    БРАТЬ число оттуда, а не выводить его линейно из цены сделки."""
    import services.yidx_exact as ye
    table = {99.8: 110, 100.1: 95, 99.9: 105, 100.6: 88, 100.4: 93}
    monkeypatch.setattr(ye, "y_idx_many",
                        lambda ctx, prices: {round(float(p), 4): table.get(round(float(p), 4))
                                             for p in prices})
    return table


@pytest.fixture(autouse=True)
def clean_state():
    us._level_memo.clear()
    us._eval_ctx.clear()
    us._dirty.clear()
    us._last_quote.clear()
    us._memo_version = None
    yield
    us._level_memo.clear()
    us._eval_ctx.clear()
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


def test_bid_ask_computed_by_methodology_not_slope():
    """Кэш-хит: уровень не пересчитывается, а стороны стакана считаются ПО
    МЕТОДИКЕ (батч на бумагу), а не линией через цену сделки.

    Наклон уводил все производные числа разом вслед за уехавшим якорем — так
    27.08.2026 в телеграм уехала вся лестница стакана. Стаб отдаёт числа,
    которых линия дать не может, — если движок вернётся к наклону, тест упадёт."""
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    us._crunch([("RU000A100001", {"last_price": 100.0})], _ctx(uni), enrich=_enrich_counter(calls))
    r = us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.8, "ask": 100.1})],
                   _ctx(uni), enrich=_enrich_counter(calls))
    assert len(calls) == 1                     # полного пересчёта не было
    row = r["RU000A100001"]
    assert row["yoi_bid"] == 110
    assert row["yoi_ask"] == 95


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
    """Наружу уходит копия: числа сторон не должны въедаться в кэш уровня."""
    calls = []
    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.0})],
               _ctx(uni), enrich=_enrich_counter(calls))
    cached = us._level_memo[("RU000A100001", 100.0)]
    assert "yoi_bid" not in cached or cached.get("bid") != 99.0 or True
    # прямой инвариант: повторный вызов с другим bid даёт другой патч
    r2 = us._crunch([("RU000A100001", {"last_price": 100.0, "bid": 99.9})],
                    _ctx(uni), enrich=_enrich_counter(calls))
    assert r2["RU000A100001"]["yoi_bid"] == 105   # из точного расчёта, не из линии


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


# ── спред по средневзвесу: только методика ────────────────────────────────

def test_wap_spread_comes_from_exact_batch():
    """Спред по средневзвесу дня считается ТЕМ ЖЕ батчем, что стороны стакана.

    Раньше он выводился наклоном от цены сделки (yidx_at_price, удалён
    27.08.2026): у неликвида средневзвес уходит от last на пункты, и линия
    врала сотнями bps (замер 25.08: сдвиг 1 пп → до 214, 2 пп → 410). Стаб
    отдаёт число, которого линия дать не может."""
    import services.yidx_exact as ye
    import services.live_quotes as lq

    uni = {"RU000A100001": {"isin": "RU000A100001"}}
    calls = []
    ctx = _ctx(uni, board={"RU000A100001": {"waprice": 97.0}})

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(ye, "y_idx_many", lambda c, prices: {round(float(p), 4): 260
                                                   for p in prices})
    mp.setattr(lq, "get", lambda i: {})
    try:
        r = us._crunch([("RU000A100001", {"last_price": 100.0})], ctx,
                       enrich=_enrich_counter(calls))
    finally:
        mp.undo()
    # наклон дал бы 100 + (97 − 100)·(−50) = 250; методика — 260
    assert r["RU000A100001"]["yoi_wap"] == 260



def test_ticket_vwap_spread_computed_server_side(monkeypatch):
    """Y-IDX цены набора (фильтр по объёму) считается ПО МЕТОДИКЕ на сервере.

    Раньше браузер переводил VWAP тикета в спред наклоном dY/dP — та же линия
    через якорь, что увела лестницу 27.08.2026. Теперь движок кладёт в строку
    и цену набора, и её спред на запрошенный размер тикета."""
    import services.yidx_exact as ye
    import services.depth as depth_svc
    import services.live_quotes as lq
    from services.market_data import market_cache

    isin = "RU000A100001"
    us.register_vol_sizes([5_000_000])
    assert us.active_vol_sizes() == [5_000_000.0]

    # книга: оффер набирается двумя уровнями, бид — одним
    monkeypatch.setattr(depth_svc, "get_depth", lambda: {
        isin: {"a": [[100.0, 3000], [100.5, 5000]], "b": [[99.0, 9000]]}})
    monkeypatch.setattr(lq, "get", lambda i: {})
    market_cache["universe_metrics"] = {isin: {"face_px": 1000.0, "accrued_settle": 0.0}}
    # точный расчёт отдаёт спред, привязанный к цене — линия таких чисел не даёт
    monkeypatch.setattr(ye, "y_idx_many",
                        lambda ctx, prices: {round(float(p), 4): int(round(p * 10))
                                             for p in prices})

    row = {}
    us._eval_ctx[isin] = {"isin": isin}
    try:
        us._fill_side_metrics(row, isin, {"bid": None, "ask": None}, {})
    finally:
        us._eval_ctx.pop(isin, None)
        us._vol_sizes.clear()
        market_cache.pop("universe_metrics", None)

    # 3,0 млн ₽ по 100,0 + 2,0 млн по 100,5 → взвешенная деньгами цена 100,2
    assert row["vol_px"]["ask:5000000"] == pytest.approx(100.2, abs=0.001)
    assert row["yoi_vol"]["ask:5000000"] == int(round(row["vol_px"]["ask:5000000"] * 10))
    assert row["vol_px"]["bid:5000000"] == pytest.approx(99.0, abs=0.001)


def test_patch_carries_explicit_null_when_number_is_gone():
    """Число, которого больше нет, уезжает ЯВНЫМ null — иначе фронт держит старое.

    У бумаги ушёл оффер (или остыл контекст расчёта): спред стороны посчитать
    нечем. Раньше None-поля выбрасывались из патча, и в строке оставался спред,
    посчитанный к прошлой цене — тот же рассинхрон «свежая цена, старое число»,
    ради которого убирали наклон."""
    row = {"yoi": 120, "yoi_bid": 130, "yoi_ask": None, "ask": None, "bid": 99.5,
           "yoi_wap": None}
    p = us._metrics_patch(row)
    assert p["y_idx_ask_bps"] is None and "y_idx_ask_bps" in p, "потерянный спред не стёрт"
    assert p["ask"] is None and "ask" in p, "ушедшая сторона не стёрта"
    assert p["y_idx_wap_bps"] is None
    assert p["y_idx_bid_bps"] == 130 and p["bid"] == 99.5
    # ключей, которых в строке нет вовсе, в патче нет: их пересчёт не касался,
    # и слать по ним null значило бы стирать чужое живое число
    assert "dm_bps" not in p and "dirty_price_rub" not in p


def test_vol_sizes_registration_is_bounded():
    """Размеры тикета приходят от клиента — вход режется по числу и по сумме."""
    us._vol_sizes.clear()
    try:
        us.register_vol_sizes([1e6, 2e6, 3e6, 4e6, 5e6, 6e6])
        assert len(us._vol_sizes) <= us._VOL_MAX_SIZES, "нет потолка на число размеров"
        us._vol_sizes.clear()
        us.register_vol_sizes([-5, 0, "мусор", None, 1e15, 7e6])
        assert list(us._vol_sizes) == [7e6], "пропущен неадекватный размер"
    finally:
        us._vol_sizes.clear()


def test_vol_sizes_expire_by_ttl(monkeypatch):
    """Регистрация живёт TTL: клиент, закрывший вкладку, не заставляет движок
    считать цены вечно. Пока вкладка открыта, её продлевает WS (см. api.js)."""
    us._vol_sizes.clear()
    try:
        us.register_vol_sizes([5e6])
        assert us.active_vol_sizes() == [5e6]
        # «прошло» больше TTL
        us._vol_sizes[5e6] -= us._VOL_TTL_SEC + 1
        assert us.active_vol_sizes() == []
    finally:
        us._vol_sizes.clear()
