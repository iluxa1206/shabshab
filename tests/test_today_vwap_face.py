"""Цена сегодняшнего часового бара не должна зависеть от FACEVALUE.

ISS не отдаёт номинал за СЕГОДНЯ (дневная строка history публикуется после
закрытия, /securities до конца дня показывает вчерашний). У бумаг с амортизацией
сегодня и с валютным/индексируемым номиналом вчерашний номинал уводил
средневзвес дня на проценты мимо диапазона сделок (RU000A108C58 13.08.2026:
89.44 при сделках 100.46–101.14).
"""
import importlib
from datetime import date

import pytest


@pytest.fixture
def mods(tmp_path, monkeypatch):
    """services.bars + services.trades_archive на пустой временной БД."""
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.bars as bars
    import services.trades_archive as ta
    importlib.reload(bars)
    importlib.reload(ta)
    yield bars, ta
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(bars)
    importlib.reload(ta)


def _tick(pdb_mod, isin, tid, ts, price, qty, side="buy"):
    with pdb_mod._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (isin, tid, ts, price, qty, qty * price * 10, side, "TQCB"))


def test_implied_face_from_single_price_hour(mods):
    """Час с единой ценой даёт номинал точно: value/volume = цена одной бумаги."""
    bars, _ = mods
    # RU000A108C58 13.08.2026: 1 бумага по 100.48 за 225.48 ₽ → номинал 224.4
    face = bars._implied_face([
        {"volume": 1, "value": 225.48, "high": 100.48, "low": 100.48},
        {"volume": 145, "value": 32713.74, "high": 101.14, "low": 99.9},
    ])
    assert face == pytest.approx(224.4, abs=0.05)


def test_implied_face_none_without_flat_hour(mods):
    bars, _ = mods
    assert bars._implied_face([{"volume": 10, "value": 1000.0,
                                "high": 101.0, "low": 99.0}]) is None


def test_tick_vwap_hours_ignores_face(mods):
    """Средневзвес часа по тикам — Σ(цена·кол-во)/Σкол-во, номинал не участвует."""
    bars, _ = mods
    import services.portfolio_db as pdb
    day = date.today().isoformat()
    _tick(pdb, "X", 1, f"{day} 10:15:00", 100.0, 1)
    _tick(pdb, "X", 2, f"{day} 10:45:00", 102.0, 3)
    _tick(pdb, "X", 3, f"{day} 11:05:00", 99.0, 2)
    got = bars.tick_vwap_hours("X", day)
    assert got[f"{day} 10:00"] == pytest.approx((100.0 + 3 * 102.0) / 4)
    assert got[f"{day} 11:00"] == pytest.approx(99.0)


def test_spread_avg_map_weights_by_turnover_and_skips_today(mods):
    """База ОТКЛ 7Д: средневзвешенный по обороту спред ПРОШЛЫХ дней, без сегодня."""
    from datetime import timedelta
    bars, _ = mods
    import services.portfolio_db as pdb
    d1 = (date.today() - timedelta(days=2)).isoformat()
    d2 = (date.today() - timedelta(days=1)).isoformat()
    today = date.today().isoformat()
    rows = [                      # ts, y_idx_bps, value
        (f"{d1} 10:00", 100.0, 1e6),
        (f"{d2} 10:00", 200.0, 3e6),      # веса 1:3 → база 175
        (f"{today} 10:00", 900.0, 9e6),   # сегодня в базу не входит
    ]
    with pdb._lock, pdb._connect() as c:
        c.executemany("INSERT INTO bar_hourly(isin,ts,kind,y_idx_bps,value) "
                      "VALUES('X',?,'floater',?,?)", rows)
        # бумага без спреда в барах — базы нет, а не ноль
        c.execute("INSERT INTO bar_hourly(isin,ts,kind,value) "
                  "VALUES('Y',?,'floater',5e6)", (f"{d2} 11:00",))

    m = bars.spread_avg_map(7)
    assert m["X"] == pytest.approx(175.0)
    assert "Y" not in m


def test_spread_avg_map_window_cuts_old_days(mods):
    from datetime import timedelta
    bars, _ = mods
    import services.portfolio_db as pdb
    old = (date.today() - timedelta(days=9)).isoformat()
    with pdb._lock, pdb._connect() as c:
        c.execute("INSERT INTO bar_hourly(isin,ts,kind,y_idx_bps,value) "
                  "VALUES('X',?,'floater',500,1e6)", (f"{old} 10:00",))
    bars._spread_avg_cache.update(key=None, at=0.0, map={})
    assert bars.spread_avg_map(7) == {}


def test_side_vwap_from_tick_price(mods):
    """buy_vwap/sell_vwap считаются по цене тика, а не через номинал бара:
    у бара номинал вчерашний, и деление на него врало в день смены номинала."""
    bars, ta = mods
    import services.portfolio_db as pdb
    day = date.today().isoformat()
    ts = f"{day} 10:00"
    with pdb._lock, pdb._connect() as c:
        c.execute("INSERT INTO bar_hourly(isin,ts,kind,face,volume,value) "
                  "VALUES('X',?, 'floater', 252.1, 4, 900.0)", (ts,))
    _tick(pdb, "X", 1, f"{day} 10:15:00", 100.0, 1, "buy")
    _tick(pdb, "X", 2, f"{day} 10:45:00", 102.0, 3, "buy")
    _tick(pdb, "X", 3, f"{day} 10:50:00", 99.0, 2, "sell")

    assert ta.enrich_bars_with_ticks("X", frm=day) == 1
    with pdb._connect() as c:
        r = dict(c.execute("SELECT * FROM bar_hourly WHERE isin='X'").fetchone())
    assert r["buy_vwap"] == pytest.approx((100.0 + 3 * 102.0) / 4, abs=1e-4)
    assert r["sell_vwap"] == pytest.approx(99.0, abs=1e-4)
    assert r["trades"] == 3
