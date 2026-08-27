"""База цен вкладки АНАЛИТИКА: все графики — от СРЕДНЕВЗВЕСА дня.

Было (прод, до 2026-08-26): scatter и box-графики считались по средневзвесу
(`y_idx_wap_bps`), а линия «R-spread ДИНАМИКА» читалась из `spread_daily`, где
спред посчитан по цене ЗАКРЫТИЯ — вечерним снапшотом или honest-бэкфиллом по
close свечи. Сегодняшняя точка линии при этом достраивалась фронтом по
средневзвесу: три графика одной вкладки на двух базах, и последняя точка линии
на третьей. Разница по бумаге 4–10 б.п., по медиане бакета — до 18.

Здесь проверяется, что дневная свёртка несёт горизонт (без него линию по
средневзвесу строить нельзя — спред к оферте сложился бы со спредом к
погашению) и что спред ко второму горизонту взвешен тем же оборотом.
"""
import importlib

import pytest

from services.bars import BARS_METRICS_VERSION as _BV


@pytest.fixture
def bars(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.bars as bars_mod
    importlib.reload(bars_mod)
    yield bars_mod
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(bars_mod)


def _hour(ts, *, vwap, close, y, y_close, value, horizon="maturity",
          y_alt=None, alt_horizon="put"):
    return {"isin": "RU_T", "ts": ts, "kind": "floater", "open": vwap, "high": vwap,
            "low": vwap, "close": close, "vwap_pct": vwap, "volume": value / 10,
            "value": value, "face": 1000.0, "y_idx_bps": y, "dm_bps": None,
            "g_spread_bps": None, "ytm": None, "y_open_bps": y, "y_high_bps": y,
            "y_low_bps": y, "y_close_bps": y_close, "metrics_ver": 8,
            "horizon": horizon, "y_idx_alt_bps": y_alt, "alt_horizon": alt_horizon}


def test_daily_rollup_keeps_horizon(bars):
    """Горизонт дня доезжает до bar_daily: без него медианная линия аналитики
    сложила бы спред к оферте со спредом к погашению."""
    bars.upsert_bars([_hour("2026-08-25 11:00", vwap=100.0, close=100.0, y=200,
                            y_close=200, value=1_000_000, horizon="put",
                            y_alt=340, alt_horizon="maturity")])
    assert bars.build_daily("RU_T") == 1
    row = bars.read_daily("RU_T")[0]
    assert row["horizon"] == "put" and row["alt_horizon"] == "maturity"
    assert row["y_idx_alt_wap_bps"] == 340


def test_alt_spread_weighted_by_same_turnover(bars):
    """Спред ко второму горизонту взвешивается ТЕМ ЖЕ оборотом, что и основной:
    обе ветки обязаны отвечать одной и той же средневзвешенной цене дня."""
    bars.upsert_bars([
        _hour("2026-08-25 11:00", vwap=100.0, close=100.0, y=100, y_close=100,
              value=1_000_000, y_alt=300),
        _hour("2026-08-25 15:00", vwap=101.0, close=101.0, y=200, y_close=200,
              value=3_000_000, y_alt=500),
    ])
    bars.build_daily("RU_T")
    row = bars.read_daily("RU_T")[0]
    # веса 1:3 → основной 175, альтернативный 450
    assert row["y_idx_wap_bps"] == 175.0
    assert row["y_idx_alt_wap_bps"] == 450.0
    assert row["wap_pct"] == pytest.approx(100.75)


def test_hour_without_turnover_out_of_average(bars):
    """Час без оборота в средневзвес не идёт, но горизонт из него берётся:
    цена без сделок не мнение рынка, а горизонт — свойство выпуска."""
    bars.upsert_bars([
        _hour("2026-08-25 11:00", vwap=100.0, close=100.0, y=100, y_close=100,
              value=2_000_000, horizon="maturity"),
        _hour("2026-08-25 16:00", vwap=None, close=105.0, y=None, y_close=900,
              value=0, horizon="call"),
    ])
    bars.build_daily("RU_T")
    row = bars.read_daily("RU_T")[0]
    assert row["y_idx_wap_bps"] == 100.0        # пустой час не сдвинул средневзвес
    assert row["horizon"] == "call"             # горизонт — из последнего часа


def test_aggregate_reads_vwap_rollup(bars, monkeypatch):
    """Эндпоинт динамики берёт спред по средневзвесу из bar_daily и режет точки
    чужого горизонта. Раньше он читал spread_daily (цена закрытия) — это и был
    разъезд баз внутри одной вкладки."""
    import asyncio
    import importlib
    import api.routes.history as hist
    importlib.reload(hist)

    from datetime import date, timedelta
    d0 = (date.today() - timedelta(days=3)).isoformat()
    d1 = (date.today() - timedelta(days=2)).isoformat()
    import services.portfolio_db as pdb
    with pdb._connect() as c:
        c.executemany(
            # версия ТЕКУЩАЯ, а не зашитое число: агрегат отдаёт только строки
            # актуального движка (медиана по разным версиям — среднее по двум
            # методикам), и с константой тест ломался бы на каждом бампе
            "INSERT INTO bar_daily(isin,date,kind,wap_pct,y_idx_wap_bps,"
            "y_idx_alt_wap_bps,horizon,alt_horizon,metrics_ver) "
            f"VALUES(?,?,?,?,?,?,?,?,{_BV})",
            [("RU_A", d0, "floater", 100.0, 200.0, 340.0, "maturity", "put", ),
             ("RU_A", d1, "floater", 100.2, 210.0, 350.0, "maturity", "put"),
             # у второй бумаги день посчитан к ОФЕРТЕ, а последний известный
             # горизонт — погашение: берётся альтернативная ветка той же строки
             ("RU_B", d0, "floater", 99.0, 900.0, 250.0, "put", "maturity"),
             ("RU_B", d1, "floater", 99.1, 905.0, 255.0, "maturity", "put")])

    # реестр эндпоинт импортирует внутри функции — подменяем сам модуль
    import services.instruments_registry as reg
    monkeypatch.setattr(reg, "universe_rows",
                        lambda *a, **k: [{"isin": "RU_A", "rating": "AAA"},
                                         {"isin": "RU_B", "rating": "AAA"}])
    res = asyncio.run(hist.yidx_aggregate(hist.YidxAggBody(days=30, by="rating")))
    pts = {p["date"]: p["med"] for p in res["series"][0]["points"]}
    # d0: RU_A 200 (своя ветка) + RU_B 250 (альтернативная, к погашению) → 225
    assert pts[d0] == 225.0
    assert pts[d1] == 557.5     # 210 и 905 — обе к погашению
