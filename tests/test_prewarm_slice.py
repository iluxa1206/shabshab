"""Прогрев универса режется по времени, а не считает всё одним заходом."""
import time
import pytest


def test_prewarm_crunch_returns_pending(monkeypatch):
    """Заход прогрева кончается ПО ВРЕМЕНИ и отдаёт недосчитанных.

    Счёт держит GIL: проход по всем 611 бумагам одним заходом — десятки секунд,
    в которые сайт не отвечает (замер 02.09: восемь медленных запросов за
    прогрев). Недосчитанные обязаны вернуться вызывающему, иначе рынок
    недосчитается молча."""
    import services.universe as un

    calls = []

    def fake_enrich(u, ref, full, **kw):
        calls.append(u["isin"])
        return {"yoi": 100}

    ids = [f"RU000A1000{i:02d}" for i in range(5)]
    uni = [{"isin": i, "base_rate_type": "KEYRATE"} for i in ids]

    async def _a(v):
        return v

    monkeypatch.setattr(un, "enrich_bond", fake_enrich)
    monkeypatch.setattr(un, "build_universe_ref", lambda u, i, c, s: object())
    monkeypatch.setattr(un.MarketDataService, "get_local_bond_cache",
                        staticmethod(lambda p: {i: {} for i in ids}))
    monkeypatch.setattr(un.MarketDataService, "fetch_board_snapshot",
                        staticmethod(lambda: _a({})))
    monkeypatch.setattr(un.MarketDataService, "get_curves",
                        staticmethod(lambda: _a((None, None, None, None))))
    monkeypatch.setattr(un.MarketDataService, "get_zspread_ctx",
                        staticmethod(lambda: _a((None, None, None))))
    monkeypatch.setattr(un.MarketDataService, "session_prices",
                        staticmethod(lambda: {i: 100.0 for i in ids}))
    monkeypatch.setattr(un.MarketDataService, "flush_schedule_cache",
                        staticmethod(lambda: None))
    # слайс в прошлом: каждый заход считает одну бумагу и выходит
    monkeypatch.setattr(un, "_PREWARM_SLICE_SEC", -1.0)

    # считаем ЗАХОДЫ в поток: нарезка видна именно по ним
    import asyncio
    import services.heavy as heavy
    runs = []
    real_run = heavy.run_heavy

    async def counting_run(fn, *a, **kw):
        runs.append(getattr(fn, "__name__", "?"))
        return await real_run(fn, *a, **kw)

    monkeypatch.setattr(heavy, "run_heavy", counting_run)

    res = asyncio.run(un.compute_universe_metrics(uni, ids, "x.json"))
    # весь универс досчитан, несмотря на нарезку по одной бумаге за заход
    assert len(res) == len(ids)
    assert calls == ids
    # пять бумаг при слайсе в прошлом = пять заходов счёта (+ backfill),
    # а не один длинный, в котором ядро молчит целиком
    assert runs.count("_crunch") == len(ids)
