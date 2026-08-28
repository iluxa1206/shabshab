"""ФИКСЫ в событийном движке метрик (пул котировок/стаканов Alor).

Витрина фиксов — тот же монитор, что у флоатеров, поэтому и живут они на одном
пуле: цена сделки и стороны стакана приходят пушем, движок пересчитывает по ним
YTM и g-спред. Отличий два, и оба здесь проверяются:

1. У фикса СВОЯ математика (compute_fixed_row), а не enrich_bond — и своя
   витрина: строки уезжают в market_cache['fixed_metrics'], а не в
   'universe_metrics' (схемы строк разные, один словарь на двоих их бы смешал).
2. У фикса НЕТ дешёвой очереди сторон: один проход считает и цену сделки, и
   bid/ask/средневзвес, поэтому движение стакана заказывает полный пересчёт.
"""
from datetime import date

import asyncio
import pytest

from api.routes.calc import build_custom_schedule, _accrued
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
    us._dirty.clear(); us._sides_dirty.clear(); us._last_quote.clear()
    us._fixed_isins.clear()
    yield
    us._dirty.clear(); us._sides_dirty.clear(); us._last_quote.clear()
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


def test_side_move_asks_full_recount_for_fixed(monkeypatch):
    """Движение стакана у фикса — в полную очередь: дешёвой ветки сторон у него
    нет (у флоатера она есть и остаётся)."""
    monkeypatch.setattr(us, "_broadcast_quote", lambda isin, data: asyncio.sleep(0))
    us._fixed_isins.add("RU000A1FIX01")
    both = ("RU000A1FIX01", "RU000A1FLT01")
    for isin in both:      # первая котировка: новая цена сделки — полный пересчёт
        asyncio.run(us._on_quote(isin, {"last_price": 100.0, "bid": 99.0, "ask": 100.5}))
    us._dirty.clear()
    for isin in both:      # вторая: цена сделки та же, сдвинулся только бид
        asyncio.run(us._on_quote(isin, {"last_price": 100.0, "bid": 99.2, "ask": 100.5}))
    assert us._dirty == {"RU000A1FIX01"}
    assert set(us._sides_dirty) == {"RU000A1FLT01"}


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
