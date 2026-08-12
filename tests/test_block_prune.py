"""Ретеншен слоя крупных сделок: сегодня — весь рынок, в архиве — от порога.

Полный поток ISS это ~272k сделок за день; держать его вечно незачем, а внутри
дня он нужен целиком: мелкие принты по бумагам вне юниверса и адресные сделки
больше нигде не сохраняются.
"""
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture()
def bt(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.block_trades as mod
    importlib.reload(mod)
    return pdb, mod


def _seed(pdb, rows):
    """rows: [(trade_id, день, сумма)]"""
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,qty,"
            "value,yld,side,face,cur) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(tid, "RU000A0000A1", "RU000A0000A1", f"{d} 10:00:{i:02d}", "bonds",
              "TQCB", 100.0, 1, val, None, "buy", 1000, "SUR")
             for i, (tid, d, val) in enumerate(rows)])


def _ids(pdb):
    with pdb._connect() as c:
        return sorted(r[0] for r in c.execute("SELECT trade_id FROM block_trade"))


def test_keeps_today_whole_and_archives_big(bt):
    pdb, mod = bt
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=1)).isoformat()
    big = mod.BLOCK_ARCHIVE_MIN_RUB
    _seed(pdb, [(1, today, 10_000),        # сегодня мелочь — остаётся
                (2, today, big + 1),       # сегодня крупная — остаётся
                (3, old, 10_000),          # вчера мелочь — под нож
                (4, old, big - 1),         # вчера ниже порога — под нож
                (5, old, big)])            # вчера ровно порог — остаётся

    assert mod.prune(dry_run=True)["deleted"] == 2      # считает, но не трогает
    assert _ids(pdb) == [1, 2, 3, 4, 5]

    res = mod.prune()
    assert res["deleted"] == 2 and res["archive_min_rub"] == big
    assert _ids(pdb) == [1, 2, 5]
    assert mod.prune()["deleted"] == 0                 # идемпотентно


def test_raw_days_widens_window(bt):
    """BLOCK_RAW_DAYS=2 — вчера тоже держим целиком."""
    pdb, mod = bt
    today = date.today().isoformat()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    d2 = (date.today() - timedelta(days=2)).isoformat()
    _seed(pdb, [(1, today, 1), (2, d1, 1), (3, d2, 1)])
    assert mod.prune(raw_days=2)["deleted"] == 1
    assert _ids(pdb) == [1, 2]


def test_yidx_queue_skips_small(bt):
    """Очередь спреда не берёт мелочь: солвер по всему потоку не нужен."""
    pdb, mod = bt
    today = date.today().isoformat()
    _seed(pdb, [(1, today, 1_000), (2, today, mod.BLOCK_YIDX_MIN_RUB)])
    assert [r["trade_id"] for r in mod.unpriced()] == [2]
    assert mod.unpriced_count() == 1
