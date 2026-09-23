"""ФИКСЫ в событийном движке метрик (пул котировок/стаканов Alor).

Витрина фиксов — тот же монитор, что у флоатеров, поэтому и живут они на одном
пуле: цена сделки и стороны стакана приходят пушем, движок пересчитывает по ним
YTM и g-спред. Отличий два, и оба здесь проверяются:

1. У фикса СВОЯ математика (compute_fixed_row), а не enrich_bond — и своя
   витрина: строки уезжают в market_cache['fixed_metrics'], а не в
   'universe_metrics' (схемы строк разные, один словарь на двоих их бы смешал).
2. У фикса есть отдельный точный патч сторон: движение стакана не трогает last,
   WAP и Z-спред, но YTM/G-спред нового BID/ASK считает без наклона.
"""
from datetime import date

import asyncio
import pytest

from services.custom_bond import build_custom_schedule, _accrued
from core.valuation import settle_date
from services import universe_stream as us


CD = date(2026, 8, 6)


@pytest.fixture
def fixed_row():
    sched = build_custom_schedule(date(2030, 3, 15), 13.0, 2, 1000.0, CD)
    row = {"isin": "RU000A1FIX01", "secid": "FIX01", "name": "ФИКС 1Р-01",
           "cls": "corp", "face": 1000.0, "coupon_pct": 13.0, "prev": 99.0,
           "accrued": _accrued(sched, settle_date(CD), CD)}
    return row, sched


@pytest.fixture(autouse=True)
def clean_state():
    us._dirty.clear(); us._sides_dirty.clear(); us._fixed_sides_dirty.clear(); us._last_quote.clear()
    us._fixed_sides_wake.clear()
    us._fixed_isins.clear()
    yield
    us._dirty.clear(); us._sides_dirty.clear(); us._fixed_sides_dirty.clear(); us._last_quote.clear()
    us._fixed_sides_wake.clear()
    us._fixed_isins.clear()


def _ctx(fixed_by, full_by):
    return {"uni_by": {}, "fixed_by": fixed_by, "cache": {}, "secs": {},
            "board": {}, "ruonia_curve": None, "keyrate_curve": None,
            "exp_ks": None, "exp_ru": None, "g_curve": None,
            "calc_date": CD, "version": ("2026-08-06", 1.0), "full_by": full_by}


def test_fixed_branch_computes_own_metrics(fixed_row):
    """Бумага не из флоатер-юниверса, но из универса фиксов — считается своей
    математикой, а не пропускается."""
    row, sched = fixed_row
    ctx = _ctx({row["isin"]: row}, {row["isin"]: sched})
    out = us._crunch([(row["isin"], {"last_price": 99.5, "bid": 99.4, "ask": 99.6})], ctx)
    got = out[row["isin"]]
    assert got["_kind"] == "fixed"
    # доходность считается и по цене сделки, и по сторонам стакана
    assert got["ytm"] is not None
    assert got["ytm_bid"] > got["ytm"] > got["ytm_ask"]
    assert got["last"] == 99.5 and got["bid"] == 99.4


def test_fixed_rows_go_to_fixed_cache(fixed_row):
    """Строки такта раскладываются по своим витринам."""
    row, _ = fixed_row
    cache = {}
    us._store_rows(cache, {"RU000A1FIX01": {"_kind": "fixed", "ytm": 15.0},
                           "RU000A1FLT01": {"yoi": 200}})
    assert list(cache["fixed_metrics"]) == ["RU000A1FIX01"]
    assert list(cache["universe_metrics"]) == ["RU000A1FLT01"]


def test_daily_delta_survives_tick():
    """Δ YTM — дневной срез, от цены не зависит: тик не должен стирать колонку."""
    cache = {"fixed_metrics": {"RU000A1FIX01": {"ytm": 14.0, "delta_ytm": -0.2}}}
    us._store_rows(cache, {"RU000A1FIX01": {"_kind": "fixed", "ytm": 14.1}})
    assert cache["fixed_metrics"]["RU000A1FIX01"]["delta_ytm"] == -0.2


def test_fixed_partial_side_patch_keeps_full_row():
    """Патч сторон (дешёвая ветка) несёт только bid/ask и их числа: запись в
    витрину обязана СЛИТЬ его с прежней строкой, а не заменить её. Иначе после
    каждого движения стакана /api/fixed отдавал цену и прочерки по YTM,
    g-спреду, дюрации — до перезагрузки, заставшей полный пересчёт."""
    cache = {"fixed_metrics": {"RU000A1FIX01": {
        "last": 99.5, "ytm": 14.0, "g_spread_bps": 120.0, "mod_dur": 2.3,
        "dirty": 1010.0, "bid": 99.3, "ytm_bid": 14.1, "g_spread_bid_bps": 130.0}}}
    us._store_rows(cache, {"RU000A1FIX01": {
        "_kind": "fixed", "bid": 99.2, "ask": 99.7,
        "ytm_bid": 14.2, "ytm_ask": 13.9,
        "g_spread_bid_bps": 140.0, "g_spread_ask_bps": 110.0}})
    row = cache["fixed_metrics"]["RU000A1FIX01"]
    assert row["bid"] == 99.2 and row["ytm_bid"] == 14.2          # своё обновлено
    assert row["last"] == 99.5 and row["ytm"] == 14.0             # чужое цело
    assert row["g_spread_bps"] == 120.0 and row["mod_dur"] == 2.3
    assert row["dirty"] == 1010.0
    # явный null полного пересчёта — стирает («считать стало нечем»)
    us._store_rows(cache, {"RU000A1FIX01": {"_kind": "fixed", "ytm": None}})
    assert cache["fixed_metrics"]["RU000A1FIX01"]["ytm"] is None


def test_fixed_sides_batch_fetches_missing_schedules(fixed_row, monkeypatch):
    """Ветка сторон обязана сама добыть расписание бумаги, которой нет в
    full_by (его заполняет только полный пересчёт своей пачкой): иначе патча
    сторон нет, и прочерк у ликвидной ОФЗ живёт, пока идут котировки."""
    from services.market_data import MarketDataService
    row, sched = fixed_row
    ctx = _ctx({row["isin"]: row}, {"RU000A1OTHER": {"coupons": [1]}})
    calls = []

    async def fake_full(key):
        calls.append(key)
        return sched
    monkeypatch.setattr(MarketDataService, "fetch_bond_schedule_full", staticmethod(fake_full))
    asyncio.run(us._ensure_full_by(ctx, [row["isin"], "RU000A1OTHER"]))
    assert calls == [row["secid"]]                       # только недостающая, по SECID
    assert ctx["full_by"][row["isin"]] is sched
    assert ctx["full_by"]["RU000A1OTHER"] == {"coupons": [1]}   # чужое цело (слияние)
    # повторный вызов — без сети
    asyncio.run(us._ensure_full_by(ctx, [row["isin"]]))
    assert len(calls) == 1


def test_fixed_warm_merge_keeps_engine_rows_of_the_day():
    """Прогрев фиксов не затирает строку, которую движок посчитал в этот
    торговый день: его цена — живой поток, у прогрева — борд с задержкой."""
    import time as _t
    since = _t.mktime((2026, 9, 23, 0, 0, 0, 0, 0, -1))
    cache = {"fixed_metrics": {
        # движок считал сегодня — остаётся, прогрев доливает Δ YTM и недостающее
        "RU000A1FIX01": {"last": 99.5, "ytm": 15.46, "g_spread_bps": 78,
                         "vol_px": {"bid:5000000": 99.4}, "_calc_ts": since + 3600},
        # строка вчерашняя — прогрев побеждает, поля движка переживают
        "RU000A1FIX02": {"last": 98.0, "ytm": 14.0, "vol_px": {"bid:5000000": 97.9},
                         "_calc_ts": since - 3600},
    }}
    fm = {"RU000A1FIX01": {"last": 99.76, "ytm": 15.3, "g_spread_bps": 70,
                           "delta_ytm": -0.1, "put_date": "2027-01-01"},
          "RU000A1FIX02": {"last": 98.2, "ytm": 14.1, "delta_ytm": 0.05},
          "RU000A1FIX03": {"last": 100.0, "ytm": 13.0}}
    kept, merged = us.merge_fixed_metrics(cache, fm, since)
    assert (kept, merged) == (1, 2)
    r1 = cache["fixed_metrics"]["RU000A1FIX01"]
    assert r1["last"] == 99.5 and r1["ytm"] == 15.46 and r1["g_spread_bps"] == 78
    assert r1["delta_ytm"] == -0.1 and r1["put_date"] == "2027-01-01"
    assert r1["vol_px"] == {"bid:5000000": 99.4}
    r2 = cache["fixed_metrics"]["RU000A1FIX02"]
    assert r2["last"] == 98.2 and r2["ytm"] == 14.1 and r2["vol_px"] == {"bid:5000000": 97.9}
    assert cache["fixed_metrics"]["RU000A1FIX03"]["ytm"] == 13.0


def test_side_move_queues_cheap_recount_for_fixed(monkeypatch):
    """Движение стакана у фикса идёт в ту же дешёвую очередь, что и флоатер;
    полная очередь нужна только при новой цене сделки."""
    monkeypatch.setattr(us, "_broadcast_quote", lambda isin, data: asyncio.sleep(0))
    us._fixed_isins.add("RU000A1FIX01")
    both = ("RU000A1FIX01", "RU000A1FLT01")
    for isin in both:      # первая котировка: новая цена сделки — полный пересчёт
        asyncio.run(us._on_quote(isin, {"last_price": 100.0, "bid": 99.0, "ask": 100.5}))
    us._dirty.clear()
    for isin in both:      # вторая: цена сделки та же, сдвинулся только бид
        asyncio.run(us._on_quote(isin, {"last_price": 100.0, "bid": 99.2, "ask": 100.5}))
    assert not us._dirty
    assert set(us._sides_dirty) == {"RU000A1FLT01"}
    assert set(us._fixed_sides_dirty) == {"RU000A1FIX01"}
    assert us._fixed_sides_wake.is_set()


def test_fixed_side_queue_ignores_floater_column_scope(monkeypatch):
    """YTM BID/ASK фикса не должны исчезнуть из очереди из-за scope флоатеров."""
    us._fixed_sides_dirty["RU000A1FIX01"] = us._SIDES_PRIO_LIVE
    monkeypatch.setattr(us, "sides_needed", lambda: False)
    take, _prio = us._take_fixed_sides_batch()
    assert take == ["RU000A1FIX01"]


def test_fixed_cheap_side_patch_keeps_last_metrics(fixed_row, monkeypatch):
    """Новый bid пересчитывает только свои точные поля, не зовя полный расчёт."""
    from services.market_data import market_cache
    from services import fixed_income

    row, sched = fixed_row
    ctx = _ctx({row["isin"]: row}, {row["isin"]: sched})
    ctx["board"] = {row["isin"]: {"accrued": row["accrued"], "bid": 99.2, "ask": 99.7}}
    baseline = us._crunch([(row["isin"], {"last_price": 99.5, "bid": 99.3, "ask": 99.6})], ctx)
    previous = market_cache.get("fixed_metrics")
    try:
        market_cache["fixed_metrics"] = baseline
        us._last_quote[row["isin"]] = {"bid": 99.2, "ask": 99.7}
        monkeypatch.setattr(fixed_income, "compute_fixed_row",
                            lambda *a, **k: pytest.fail("полный пересчёт вызван"))
        patch = us.recrunch_sides([row["isin"]], ctx["board"], fixed_ctx=ctx)[row["isin"]]
        assert patch["_kind"] == "fixed"
        assert patch["bid"] == 99.2 and patch["ytm_bid"] is not None
        assert "ytm" not in patch and "z_spread_bps" not in patch
    finally:
        us._last_quote.pop(row["isin"], None)
        if previous is None:
            market_cache.pop("fixed_metrics", None)
        else:
            market_cache["fixed_metrics"] = previous


def test_pool_groups_are_independent(monkeypatch):
    """Пересборка группы фиксов не рвёт сокеты флоатеров.

    Состав фиксов пересобирается по ликвидности каждый час — при общем пуле это
    гасило бы вместе с ними живые подписки флоатеров каждый раз."""
    started = []

    async def fake_socket(sid, isins, stop, *a, **kw):
        started.append(sid)
        await stop.wait()

    monkeypatch.setattr(us, "_shard_socket", fake_socket)
    monkeypatch.setattr(us, "_depth_socket", fake_socket)

    async def scenario():
        fl, fx = us._groups["floaters"], us._groups["fixed"]
        try:
            us._rebuild_group("фл", [f"RU{i:010d}" for i in range(200)], 0, fl)
            fl_tasks = list(fl["tasks"])
            us._rebuild_group("фикс", [f"SU{i:010d}" for i in range(300)],
                              us._GROUP_STRIDE, fx)
            await asyncio.sleep(0)
            assert not any(t.cancelled() or t.done() for t in fl_tasks), \
                "сокеты флоатеров пережили пересборку группы фиксов"
            # вторая пересборка фиксов — снова не трогает флоатеров
            us._rebuild_group("фикс", [f"SU{i:010d}" for i in range(150)],
                              us._GROUP_STRIDE, fx)
            await asyncio.sleep(0)
            assert not any(t.cancelled() or t.done() for t in fl_tasks)
            # номера шардов групп не пересекаются
            assert min(s for s in started if s >= us._GROUP_STRIDE) >= us._GROUP_STRIDE
            assert max(s for s in started if s < us._GROUP_STRIDE) < us._GROUP_STRIDE
        finally:
            us._stop_sockets(fl["tasks"], fl["stops"])
            us._stop_sockets(fx["tasks"], fx["stops"])
            await asyncio.sleep(0)
            for sid in list(us._shards):
                us._shards.pop(sid, None)
                us._depth_shards.pop(sid, None)

    asyncio.run(scenario())


def test_fixed_volume_ticket_price_and_spread(fixed_row, monkeypatch):
    """ФИЛЬТР ПО ОБЪЁМУ: цена набора тикета по лестнице стакана и g-спред ПО НЕЙ.

    Считает движок, а не браузер: в браузере нет ни потока, ни кривой, а спред
    по цене набора обязан считаться той же методикой, что и по цене сделки."""
    row, sched = fixed_row
    isin = row["isin"]
    from services import depth as depth_svc
    # книга: бид тонкий сверху, глубже дешевле — набор на 5 млн ₽ уедет от верха
    monkeypatch.setattr(depth_svc, "get_depth", lambda: {isin: {
        "b": [[99.5, 100], [99.0, 5000], [98.5, 20000]],
        "a": [[100.5, 100], [101.0, 5000]],
    }})
    us.register_vol_sizes([5_000_000])
    try:
        ctx = _ctx({isin: row}, {isin: sched})
        out = us._crunch([(isin, {"last_price": 100.0, "bid": 99.5, "ask": 100.5})], ctx)[isin]
        key = "bid:5000000"
        assert out["vol_px"][key] is not None, "цена набора посчитана"
        assert out["vol_px"][key] < 99.5, "набор уходит вглубь книги, ниже верха бида"
        # метрики по цене набора — свои, и доходность ВЫШЕ, чем по верху бида
        # (набор уехал вглубь, цена ниже). g-спред считается там же, но в тесте
        # кривой нет — проверяем, что ключ заполняется той же парой.
        assert out["ytm_vol"][key] > out["ytm_bid"]
        assert key in out["g_spread_vol"]
    finally:
        us._vol_sizes.clear()


def test_disabled_layer_serves_empty_without_network(monkeypatch):
    """FIXED_TAB=0: ручки фиксов отвечают пустотой и НЕ ходят в MOEX.

    Слой гасят, когда он мешает флоатерам; ходить за универсом ради витрины,
    которой на фронте уже нет, — ровно та нагрузка, от которой избавлялись."""
    import asyncio as _aio
    from api.routes import fixed as route
    from services import fixed_income as fi

    monkeypatch.setenv("FIXED_TAB", "0")

    async def boom():
        raise AssertionError("сеть не должна дёргаться при выключенном слое")

    monkeypatch.setattr(fi, "fetch_fixed_universe", boom)
    resp = _aio.run(route.get_fixed())
    assert resp["items"] == [] and resp["total"] == 0 and resp["disabled"] is True
    quotes = _aio.run(route.get_fixed_quotes())
    assert quotes["items"] == [] and quotes["disabled"] is True
