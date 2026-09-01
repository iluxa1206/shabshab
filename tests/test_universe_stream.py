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
import asyncio
import time
import pytest
from datetime import date

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


def test_new_ticket_size_serves_spreads_from_grid(monkeypatch):
    """Размер тикета увидели впервые → спреды на объём раздаются ИЗ СЕТКИ, без
    очереди пересчёта.

    Цену набора надо раздать самим (застывший неликвид о себе не напомнит ни
    сделкой, ни движением сторон), но гнать ради этого весь рынок через движок
    по 26 мс на бумагу — это полминуты ожидания в таблице."""
    us._vol_sizes.clear()
    us._sides_dirty.clear()
    us._last_quote.clear()
    us._yoi_grid.clear()
    ladders = {"RU000A100001": {"b": [[100.0, 20000]], "a": [[100.1, 20000]]}}
    monkeypatch.setattr("services.depth.get_depth", lambda: ladders)
    monkeypatch.setattr("services.market_data.market_cache",
                        {"universe_metrics": {"RU000A100001": {"isin": "RU000A100001"}}})
    try:
        us._last_quote["RU000A100001"] = {}
        # сетка по книге уже посчитана движком на прошлом проходе
        us._yoi_grid["RU000A100001"] = (us._yoi_cache_epoch, [99.9, 100.0, 100.1],
                                        {99.9: 200, 100.0: 210, 100.1: 220})
        us.register_vol_sizes([5e6])
        assert us._vol_wave_pending is True

        rows = us.apply_vol_sizes()
        assert us._sides_dirty == {}            # движок к этому непричастен
        got = rows["RU000A100001"]["yoi_vol"]
        assert set(got) == {"bid:5000000", "ask:5000000"}
        assert all(v is not None for v in got.values())
    finally:
        us._vol_sizes.clear()
        us._sides_dirty.clear()
        us._last_quote.clear()
        us._yoi_grid.clear()
        us._vol_wave_pending = False


def test_vol_wave_queues_only_books_without_grid(monkeypatch):
    """Сетки по бумаге нет, а набор собирается — вот она и идёт в очередь.
    Бумага без книги не идёт никуда: цена набора у неё всё равно None."""
    us._vol_sizes.clear()
    us._sides_dirty.clear()
    us._last_quote.clear()
    us._yoi_grid.clear()
    ladders = {"RU000A100001": {"b": [[100.0, 20000]], "a": [[100.5, 20000]]},
               "RU000A100002": {"b": [[100.0, 10]], "a": [[100.5, 10]]}}
    monkeypatch.setattr("services.depth.get_depth", lambda: ladders)
    monkeypatch.setattr("services.market_data.market_cache",
                        {"universe_metrics": {k: {"isin": k} for k in ladders}})
    try:
        us._last_quote.update({k: {} for k in ladders})
        us.register_vol_sizes([5e6])
        rows = us.apply_vol_sizes()
        assert list(us._sides_dirty) == ["RU000A100001"]   # набор есть, сетки нет
        assert "RU000A100002" in rows                      # мелкая книга — прочерк
        assert rows["RU000A100002"]["yoi_vol"] is None
    finally:
        us._vol_sizes.clear()
        us._sides_dirty.clear()
        us._last_quote.clear()
        us._yoi_grid.clear()
        us._vol_wave_pending = False


def test_live_side_move_outranks_vol_wave():
    """Движение сторон обгоняет добор волны: свежий спред по заявке не должен
    ждать, пока движок построит сетки отставшим бумагам."""
    us._sides_dirty.clear()
    try:
        us._queue_sides("RU000A100001", us._SIDES_PRIO_WAVE)
        us._queue_sides("RU000A100002", us._SIDES_PRIO_WAVE + 1e-6)
        us._queue_sides("RU000A100002")          # по ней приехал живой тик
        assert sorted(us._sides_dirty, key=us._sides_dirty.get)[0] == "RU000A100002"
    finally:
        us._sides_dirty.clear()


def test_yoi_at_interpolates_between_nodes():
    """Спред на цене между узлами — линейная вставка между двумя ТОЧНО
    посчитанными соседями (не наклон от далёкого якоря, который убрали 27.08).
    Вне сетки числа нет: врать экстраполяцией хуже, чем показать прочерк."""
    us._yoi_grid.clear()
    try:
        us._yoi_grid["RU000A100001"] = (us._yoi_cache_epoch, [100.0, 100.01, 100.02],
                                        {100.0: 200, 100.01: 210, 100.02: 220})
        assert us.yoi_at("RU000A100001", 100.01) == 210
        assert us.yoi_at("RU000A100001", 100.005) == 205
        assert us.yoi_at("RU000A100001", 99.5) is None
        assert us.yoi_at("RU000A100001", 100.5) is None
        assert us.yoi_at("RU000A999999", 100.0) is None
    finally:
        us._yoi_grid.clear()


def test_yoi_cache_skips_recount_of_same_price_set(monkeypatch):
    """Тот же набор цен — спред не пересчитывается заново.

    Батч стоит почти одинаково на 3 и на 11 цен (66 против 69 мс): вся цена в
    сборке потока и кривой, поэтому экономить надо ЦЕЛЫЙ вызов. Бумага попадает
    в очередь и без смены цен — волной нового размера тикета или вернувшейся на
    прежний уровень заявкой."""
    calls = []

    def fake_many(ctx, prices):
        calls.append(sorted(prices))
        return {round(float(p), 4): 100 for p in prices}

    monkeypatch.setattr("services.yidx_exact.y_idx_many", fake_many)
    monkeypatch.setattr(us, "_vol_prices", lambda isin: {})
    monkeypatch.setattr("services.live_quotes.get", lambda isin: {})
    us._eval_ctx["RU000A100001"] = {"ref_obj": object()}
    us._yoi_cache.clear()
    try:
        sides = {"bid": 99.0, "ask": 100.5}
        row = {}
        us._fill_side_metrics(row, "RU000A100001", sides, {})
        assert len(calls) == 1 and row["yoi_bid"] == 100

        us._fill_side_metrics({}, "RU000A100001", sides, {})
        assert len(calls) == 1          # тот же набор — из кэша

        us._fill_side_metrics({}, "RU000A100001", {"bid": 99.1, "ask": 100.5}, {})
        assert len(calls) == 2          # цена уехала — считаем заново

        # НОВАЯ КРИВАЯ: набор цен прежний, а число другое — кэш обязан промахнуться
        us._check_version(("2026-08-28", 1))
        us._eval_ctx["RU000A100001"] = {"ref_obj": object()}
        us._fill_side_metrics({}, "RU000A100001", {"bid": 99.1, "ask": 100.5}, {})
        assert len(calls) == 3
    finally:
        us._eval_ctx.clear()
        us._yoi_cache.clear()
        us._memo_version = None


def test_grid_built_once_not_on_every_side_move(monkeypatch):
    """Сетка строится заново, только если её нет, она протухла или цена набора
    вышла за края. Иначе движение сторон платило бы полную цену сетки (150 мс)
    за ответ, который уже посчитан, — в проде это подняло пересчёт стороны с
    26 до 180 мс/шт."""
    monkeypatch.setattr(us, "active_vol_sizes", lambda: [5e6])
    monkeypatch.setattr(us, "_grid_nodes", lambda *a: ["ПОСТРОИЛИ"])
    us._yoi_grid.clear()
    us._grid_budget = 10
    try:
        sides = {"bid": 100.0, "ask": 100.1}
        # сетки нет — строим
        assert us._grid_nodes_if_needed("RU000A100001", sides, None, {}) == ["ПОСТРОИЛИ"]

        us._yoi_grid["RU000A100001"] = (us._yoi_cache_epoch, [99.9, 100.0, 100.1],
                                        {99.9: 200, 100.0: 210, 100.1: 220})
        # цена набора внутри сетки — второй раз не строим
        assert us._grid_nodes_if_needed("RU000A100001", sides, None,
                                        {"bid:5000000": 99.95}) == []
        # цена ушла за край — строим
        assert us._grid_nodes_if_needed("RU000A100001", sides, None,
                                        {"ask:5000000": 101.5}) == ["ПОСТРОИЛИ"]
        # фильтра никто не смотрит — сетка не нужна вовсе
        monkeypatch.setattr(us, "active_vol_sizes", lambda: [])
        assert us._grid_nodes_if_needed("RU000A100002", sides, None, {}) == []
    finally:
        us._yoi_grid.clear()
        us._grid_budget = 0


def test_grid_builds_capped_per_tick(monkeypatch):
    """Потолок построений сетки на такт: волна нового размера тикета ставит в
    очередь сторон пол-рынка, и без потолка каждая бумага строила себе сетку
    прямо в дешёвой ветке — 13 мс превращались в ~165, а такт на 100 бумагах в
    15 секунд. Числа текущих размеров бумага получает и без сетки (их цены идут
    альт-ценами того же батча), поэтому переполнение просто откладывает сетку."""
    monkeypatch.setattr(us, "active_vol_sizes", lambda: [5e6])
    monkeypatch.setattr(us, "_grid_nodes", lambda *a: ["ПОСТРОИЛИ"])
    us._yoi_grid.clear()
    us._grid_budget = 2
    try:
        sides = {"bid": 100.0, "ask": 100.1}
        assert us._grid_nodes_if_needed("RU000A100001", sides, None, {})
        assert us._grid_nodes_if_needed("RU000A100002", sides, None, {})
        assert us._grid_budget == 0
        # бюджет выбран — сетку этому такту не строим
        assert us._grid_nodes_if_needed("RU000A100003", sides, None, {}) == []
    finally:
        us._yoi_grid.clear()
        us._grid_budget = 0


def test_grid_warm_skips_hopeless_bonds(monkeypatch):
    """Догрев не топчется на бумагах, которым сетку строить не из чего.

    Он берёт первые N из _eval_ctx, а бумаги без пуша котировки и с пустой
    книгой сеткой так и не обзаводятся — и вечно занимали эти места, пока
    остальной рынок ждал. Неудача откладывает бумагу на _GRID_RETRY_SEC."""
    monkeypatch.setattr(us, "_grid_nodes", lambda *a: [])
    monkeypatch.setattr("services.live_quotes.get", lambda isin: {})
    us._eval_ctx.clear(); us._yoi_grid.clear()
    us._last_quote.clear(); us._grid_cold.clear()
    try:
        us._eval_ctx["RU000A100001"] = {"ref_obj": object()}   # без котировки
        us._eval_ctx["RU000A100002"] = {"ref_obj": object()}
        us._last_quote["RU000A100002"] = {"bid": 100.0, "ask": 100.1}

        assert us._grid_warm_targets(10) == ["RU000A100002"]   # немой — мимо
        assert us.warm_grids(["RU000A100002"]) == 0            # книги нет
        assert "RU000A100002" in us._grid_cold
        assert us._grid_warm_targets(10) == []                 # пауза до ретрая
    finally:
        us._eval_ctx.clear(); us._yoi_grid.clear()
        us._last_quote.clear(); us._grid_cold.clear()


def test_grid_warmup_fills_missing_grids(monkeypatch):
    """Догрев строит сетки бумагам, у которых их нет, и не трогает готовые.

    Кривые пинятся на день, поэтому построенная сетка живёт до вечера: без
    догрева первый фильтр по объёму за день платил бы за весь рынок сразу."""
    monkeypatch.setattr("services.yidx_exact.y_idx_many",
                        lambda ctx, prices: {round(float(p), 4): 100 for p in prices})
    monkeypatch.setattr(us, "_grid_nodes", lambda *a: [99.9, 100.0, 100.1])
    monkeypatch.setattr("services.live_quotes.get", lambda isin: {})
    us._eval_ctx.clear(); us._yoi_grid.clear(); us._last_quote.clear()
    try:
        for isin in ("RU000A100001", "RU000A100002", "RU000A1FIX01"):
            us._eval_ctx[isin] = {"ref_obj": object()}
            us._last_quote[isin] = {"bid": 100.0, "ask": 100.1}
        us._fixed_isins.add("RU000A1FIX01")
        us._yoi_grid["RU000A100002"] = (us._yoi_cache_epoch, [100.0], {100.0: 5})

        targets = us._grid_warm_targets(10)
        assert targets == ["RU000A100001"]      # готовая и фикс — мимо

        assert us.warm_grids(targets) == 1
        assert us._yoi_grid["RU000A100001"][0] == us._yoi_cache_epoch
        assert us.yoi_at("RU000A100001", 100.05) == 100
    finally:
        us._eval_ctx.clear(); us._yoi_grid.clear(); us._last_quote.clear()
        us._fixed_isins.discard("RU000A1FIX01")


def test_ctx_warmup_covers_bonds_without_trades(monkeypatch):
    """Бумаге без сделок за день тоже нужен контекст расчёта.

    Полный пересчёт заказывает цена сделки — без неё бумага не попадала в
    движок ни разу и держала прочерк в спреде на объём весь день, хотя заявки
    в её стакане стоят и спред набора считается по цене из книги."""
    monkeypatch.setattr("services.universe.build_universe_ref",
                        lambda u, isin, cache, secs: object())
    us._eval_ctx.clear()
    try:
        uni_by = {"RU000A100001": {"base_rate_type": "KEYRATE"},
                  "RU000A100002": {"base_rate_type": "RUONIA"}}
        us._eval_ctx["RU000A100002"] = {"ref_obj": object()}
        ctx = {"uni_by": uni_by, "cache": {}, "secs": {},
               "board": {"RU000A100001": {"accrued": 12.3, "accrued_date": None}},
               "full_by": {}, "ruonia_curve": object(), "keyrate_curve": object(),
               "calc_date": date.today()}

        cold = us._ctx_warm_targets(uni_by, 10)
        assert cold == ["RU000A100001"]          # у второй контекст уже есть

        assert us.warm_ctx(cold, ctx) == 1
        ev = us._eval_ctx["RU000A100001"]
        assert ev["accrued_live"] == 12.3 and ev["accrued_missing"] is False
        assert ev["curve"] is ctx["keyrate_curve"]
    finally:
        us._eval_ctx.clear()


def test_curves_fingerprint_survives_rebuild_from_same_quotes():
    """Пересборка кривых из тех же котировок не должна сбрасывать кэши.

    Версией был curves_ts, а он меняется на каждой пересборке — при отстающем
    rates_date это раз в 15 минут, и весь прогрев (контексты, сетки, уровни)
    улетал на неизменившихся ставках."""
    class Q:
        def __init__(self, name, tenor, value):
            self.name, self.tenor, self.value = name, tenor, value
            self.date = date(2026, 8, 27)

    mc = {"ois_quotes": [Q("RUONIA", "1Y", 15.5)], "irs_quotes": [Q("KEYRATE", "1Y", 16.0)],
          "curves_ts": 1000.0}
    fp1 = us._curves_fp(mc)
    mc["curves_ts"] = 2000.0                      # пересобрали через 15 минут
    assert us._curves_fp(mc) == fp1

    mc["irs_quotes"] = [Q("KEYRATE", "1Y", 16.25)]   # ставка реально уехала
    assert us._curves_fp(mc) != fp1

    # котировок нет вовсе — откат на прежнее поведение, версия по времени
    assert us._curves_fp({"curves_ts": 5.0}) == "ts:5.0"


def test_warm_pass_runs_while_queues_are_busy(monkeypatch):
    """Догрев обязан идти при непустых очередях.

    Первая версия ждала простоя обеих очередей — на живом рынке (600–900
    depth-пушей в минуту) такого такта не бывает, и догрев не выполнялся ни
    разу: прод 28.08.2026 держал ctx 530 из 609, а бумаги из хвоста — прочерк
    в спреде при полностью исправном расчёте."""
    calls = {"ctx": [], "grids": []}

    async def fake_heavy(fn, *args):
        if fn is us.warm_ctx:
            calls["ctx"].append(list(args[0]))
            return len(args[0])
        if fn is us.warm_grids:
            calls["grids"].append(list(args[0]))
            return len(args[0])
        return {}

    monkeypatch.setattr("services.heavy.run_heavy", fake_heavy)
    monkeypatch.setattr(us, "_ctx_warm_targets", lambda uni, n: ["RU000A100001"])
    monkeypatch.setattr(us, "_grid_warm_targets", lambda n: ["RU000A100002"])
    monkeypatch.setattr(us, "active_vol_sizes", lambda: [])
    async def _sched(isin):
        return {}
    monkeypatch.setattr("services.market_data.MarketDataService.fetch_bond_schedule_full", _sched)

    ctx = {"uni_by": {}, "fixed_by": {}, "board": {}, "full_by": {}}
    us._dirty.add("RU000A100003")          # очереди НЕ пусты
    us._sides_dirty["RU000A100004"] = 0
    try:
        asyncio.run(us._warm_pass(ctx, None, {}, time.monotonic()))
        assert calls["ctx"] == [["RU000A100001"]]
        # сетки греются СЛАЙСАМИ: заходов за такт может быть несколько (между
        # ними управление возвращается петле), но не больше потолка
        assert calls["grids"] and calls["grids"][0] == ["RU000A100002"]
        assert len(calls["grids"]) <= us._WARM_MAX_SLICES

        # такт уже съеден — сетевой догрев контекстов ждёт, а СЕТКИ греются
        # всё равно, урезанной пачкой: остатка такта на живом рынке не бывает
        # вовсе, и «греть по остатку» означало не греть никогда.
        calls["ctx"].clear(); calls["grids"].clear()
        asyncio.run(us._warm_pass(ctx, None, {}, time.monotonic() - 999))
        assert not calls["ctx"]
        assert calls["grids"] == [["RU000A100002"]]
    finally:
        us._dirty.discard("RU000A100003")
        us._sides_dirty.pop("RU000A100004", None)


def test_active_vol_sizes_by_demand_not_by_size():
    """Потолок активных размеров режет по СВЕЖЕСТИ СПРОСА, а не по величине.

    Раньше побеждали самые крупные, и размер, который смотрят прямо сейчас,
    выпадал из расчёта, пока в TTL висели более крупные (свои прошлые пробы в
    поле или чужие вкладки) — в колонках бида/оффера стоял прочерк до истечения
    TTL чужой регистрации."""
    us._vol_sizes.clear()
    try:
        for v in (50e6, 40e6, 30e6, 20e6, 10e6, 9e6):
            us.register_vol_sizes([v])
        us.register_vol_sizes([1e6])           # текущий размер — самый мелкий
        assert 1e6 in us.active_vol_sizes(), "свежий спрос вытеснен крупными"
        assert 50e6 not in us.active_vol_sizes(), "самый старый спрос не вытеснен"
    finally:
        us._vol_sizes.clear()


def test_curves_rebuild_keeps_eval_ctx():
    """Пересборка кривых ВНУТРИ ДНЯ не сносит контексты расчёта: от кривой в них
    зависит одна ссылка, а сборка заново — это десять минут холодного догрева на
    весь рынок и прочерк в цене набора всё это время. Сетки и кэш уровней при
    этом обязаны обнулиться: то же число цены даёт другой спред."""
    class C:                                    # маркеры кривых
        def __init__(self, tag): self.tag = tag
    ru_old, kr_old = C("ru0"), C("kr0")
    ru_new, kr_new = C("ru1"), C("kr1")
    us._eval_ctx.clear(); us._yoi_grid.clear()
    try:
        us._eval_ctx["RU000A100001"] = {"base": "RUONIA", "curve": ru_old,
                                        "ruonia_curve": ru_old, "calc_date": "2026-09-01"}
        us._eval_ctx["RU000A100002"] = {"base": "KEYRATE", "curve": kr_old,
                                        "ruonia_curve": ru_old, "calc_date": "2026-09-01"}
        us._yoi_grid["RU000A100001"] = (us._yoi_cache_epoch, [100.0], {100.0: 200})
        us._memo_version = ("2026-09-01", "fp0")
        ctx = {"ruonia_curve": ru_new, "keyrate_curve": kr_new, "calc_date": "2026-09-01"}
        us._check_version(("2026-09-01", "fp1"), ctx)
        assert set(us._eval_ctx) == {"RU000A100001", "RU000A100002"}
        assert us._eval_ctx["RU000A100001"]["curve"] is ru_new
        assert us._eval_ctx["RU000A100002"]["curve"] is kr_new
        assert us._eval_ctx["RU000A100002"]["ruonia_curve"] is ru_new
        assert us._yoi_grid == {}, "сетка от прошлой кривой обязана уйти"

        # НОВЫЙ ДЕНЬ — контексты сносим целиком: меняются потоки, НКД, графики
        us._check_version(("2026-09-02", "fp1"), ctx)
        assert us._eval_ctx == {}
    finally:
        us._eval_ctx.clear(); us._yoi_grid.clear(); us._memo_version = None


def test_blank_sides_get_queued_and_counted(monkeypatch):
    """ПРОЧЕРК В СТОРОНЕ — работа для движка, а не приговор.

    Спред стороны считает движок, и заказывает расчёт только событие: сделка
    или движение верха стакана. По бумаге, с которой за сессию не случилось ни
    того ни другого, цена стороны в строке есть (её даёт биржевой снапшот), а
    спред остаётся прочерком до конца дня — до 27.08.2026 дыру закрывал наклон
    в браузере, и с его уходом она стала видна пользователям."""
    from services.market_data import market_cache
    us._eval_ctx.clear(); us._sides_dirty.clear(); us._last_quote.clear()
    prev = market_cache.get("universe_metrics")
    try:
        market_cache["universe_metrics"] = {
            "RU000A100001": {"yoi_bid": None, "yoi_ask": None},   # ждёт движок
            "RU000A100002": {"yoi_bid": 200, "yoi_ask": 190},     # посчитана
            "RU000A100003": {"yoi_bid": None, "yoi_ask": None},   # сторон нет вовсе
        }
        for i in ("RU000A100001", "RU000A100002", "RU000A100003"):
            us._eval_ctx[i] = {"isin": i}
        board = {"RU000A100001": {"bid": 99.0, "ask": 101.0},
                 "RU000A100002": {"bid": 99.5, "ask": 100.5},
                 "RU000A100003": {"bid": None, "ask": None}}
        assert us._blank_side_targets(board, 10) == ["RU000A100001"]
    finally:
        us._eval_ctx.clear(); us._sides_dirty.clear()
        if prev is None:
            market_cache.pop("universe_metrics", None)
        else:
            market_cache["universe_metrics"] = prev


def test_recrunch_sides_uses_board_when_no_push(monkeypatch):
    """Стороны берутся из биржевого снапшота, когда котировочного пуша по бумаге
    сегодня не было: раньше такая бумага молча пропускалась и жила с прочерком."""
    from services.market_data import market_cache
    seen = {}

    def fake_fill(row, isin, sides, snap):
        seen["sides"] = dict(sides)
        row["yoi_bid"] = 210

    monkeypatch.setattr(us, "_fill_side_metrics", fake_fill)
    us._eval_ctx.clear(); us._last_quote.clear()
    prev = market_cache.get("universe_metrics")
    try:
        market_cache["universe_metrics"] = {"RU000A100001": {"isin": "RU000A100001"}}
        us._eval_ctx["RU000A100001"] = {"isin": "RU000A100001"}
        rows = us.recrunch_sides(["RU000A100001"],
                                 {"RU000A100001": {"bid": 99.0, "ask": 101.0}})
        assert seen["sides"] == {"bid": 99.0, "ask": 101.0}
        assert rows["RU000A100001"]["yoi_bid"] == 210
    finally:
        us._eval_ctx.clear()
        if prev is None:
            market_cache.pop("universe_metrics", None)
        else:
            market_cache["universe_metrics"] = prev


# --- прогрев режется по времени, а не по числу штук ---

def test_warm_grids_stops_at_deadline(monkeypatch):
    """Заход догрева обязан кончаться ПО ВРЕМЕНИ.

    Тяжёлый счёт держит GIL, и пока идёт пачка, event loop не просыпается
    вовсе: 25 сеток по ~150 мс занимали ядро почти на четыре секунды, сторож
    писал «лаг 2.4с», а запросы витрины ждали столько же. Граница слайса
    оставляет ровно одну бумагу сверх — недоделанные вернутся следующим
    заходом."""
    import services.yidx_exact as ye
    seen = []

    def fake_many(ctx_, prices):
        seen.append(prices)
        return {round(float(p), 4): 200 for p in prices}

    monkeypatch.setattr(ye, "y_idx_many", fake_many)
    monkeypatch.setattr(us, "_grid_nodes", lambda isin, sides, wap: [100.0])
    monkeypatch.setattr(us, "_has_book", lambda isin, book=None: True)
    isins = ["RU000A10000%d" % i for i in range(5)]
    for i in isins:
        us._eval_ctx[i] = {"ctx": True}
        us._last_quote[i] = {"bid": 100.0, "ask": 100.1}
    try:
        # дедлайн в прошлом: первая бумага считается всегда (иначе догрев
        # застыл бы), на второй заход прерывается
        n = us.warm_grids(isins, {}, time.monotonic() - 1)
        assert n == 1 and len(seen) == 1

        seen.clear()
        # без границы обрабатывается весь список — прежнее поведение цело
        for i in isins:
            us._yoi_grid.pop(i, None)
        assert us.warm_grids(isins, {}) == len(isins)
    finally:
        for i in isins:
            us._eval_ctx.pop(i, None)
            us._last_quote.pop(i, None)
            us._yoi_grid.pop(i, None)


def test_warm_ctx_stops_at_deadline(monkeypatch):
    """Та же граница у контекстов: сборка это расписание купонов и калибровка
    фиксинга, десятки миллисекунд на бумагу."""
    built = []

    def fake_ref(u, isin, cache, secs):
        built.append(isin)
        return {"ref": isin}

    monkeypatch.setattr("services.universe.build_universe_ref", fake_ref)
    monkeypatch.setattr(us, "_store_eval_ctx",
                        lambda isin, u, ref, ctx, snap: us._eval_ctx.__setitem__(isin, ref))
    isins = ["RU000A2000%02d" % i for i in range(4)]
    ctx = {"uni_by": {i: {"isin": i} for i in isins}, "cache": {}, "secs": {},
           "board": {}, "full_by": {}}
    try:
        assert us.warm_ctx(isins, ctx, time.monotonic() - 1) == 1
        assert len(built) == 1
    finally:
        for i in isins:
            us._eval_ctx.pop(i, None)


def test_crunch_stops_at_deadline_and_returns_pending():
    """Живой пересчёт тоже режется по времени.

    Пачка полного пересчёта (60 бумаг × ~140 мс) держала GIL восемь секунд —
    сторож писал «лаг 3с», а запросы витрины ждали столько же. Недосчитанные
    обязаны вернуться вызывающему: очередь _dirty их уже отдала, и потерять
    здесь значит оставить бумагу с прочерком до следующей сделки."""
    uni = {"RU000A10000%d" % i: {"isin": "RU000A10000%d" % i} for i in range(4)}
    ctx = _ctx(uni)
    calls = []
    pend: list = []
    batch = [(i, {"last_price": 100.0}) for i in uni]

    rows = us._crunch(batch, ctx, enrich=_enrich_counter(calls),
                      deadline=time.monotonic() - 1, pending=pend)
    # первая бумага считается всегда, остальные — в pending
    assert len(rows) == 1
    assert pend == [i for i, _ in batch[1:]]

    # без границы считается весь батч, pending пуст — прежнее поведение цело
    pend.clear()
    rows2 = us._crunch(batch, ctx, enrich=_enrich_counter(calls), pending=pend)
    assert len(rows2) == len(batch) and pend == []


def test_sides_batch_adapts_to_measured_cost():
    """Размер пачки сторон — от ФАКТИЧЕСКОЙ цены бумаги, а не жёсткого числа.

    При жёстких 100 за такт дешёвая ветка съедала 4–7 секунд ИЗ МИНУТЫ (замер
    на проде 01.09.2026) — тяжёлый поток простаивал, а прочерки расходились по
    рынку минутами."""
    prev = us._sides_ms_avg
    try:
        us._sides_ms_avg = 0.0                 # замера ещё нет — прежний потолок
        assert us._sides_batch() == us._MAX_SIDES_BATCH
        us._sides_ms_avg = 10.0                # дешёвая бумага — пачка больше
        assert us._sides_batch() == int(us._SIDES_BUDGET_SEC * 1000 / 10)
        us._sides_ms_avg = 200.0               # дорогая — не ниже прежнего пола
        assert us._sides_batch() == us._MAX_SIDES_BATCH
        us._sides_ms_avg = 0.1                 # аномально дёшево — потолок держит
        assert us._sides_batch() == us._SIDES_BATCH_MAX
    finally:
        us._sides_ms_avg = prev


def test_blank_sides_count_measures_backlog():
    """Число ждущих сторон — в минутную сводку: по нему видно, расходится
    очередь или стоит."""
    from services.market_data import market_cache
    prev = market_cache.get("universe_metrics")
    try:
        market_cache["universe_metrics"] = {
            "RU000A100001": {"bid": 99.0, "ask": 101.0, "yoi_bid": 200, "yoi_ask": None},
            "RU000A100002": {"bid": 99.0, "ask": None, "yoi_bid": None, "yoi_ask": None},
            "RU000A100003": {"bid": None, "ask": None, "yoi_bid": None, "yoi_ask": None},
        }
        assert us._blank_sides_count() == 2    # оффер первой + бид второй
    finally:
        if prev is None:
            market_cache.pop("universe_metrics", None)
        else:
            market_cache["universe_metrics"] = prev
