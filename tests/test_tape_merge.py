"""Склейка двух архивов в одну ленту (services/tape).

Главное, что проверяем: одна и та же сделка, лежащая и в тик-архиве, и в слое
крупных сделок, попадает в ленту РОВНО ОДИН раз — ключ у обоих один (TRADENO),
и дедуп держится на нём.
"""
import importlib
from datetime import date

import pytest


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.block_trades as bt
    import services.tape as tape
    importlib.reload(bt)
    importlib.reload(tape)
    return pdb, tape


def _seed(pdb, ticks, blocks):
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
            "VALUES(?,?,?,?,?,?,?,?)", ticks)
        c.executemany(
            "INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,qty,"
            "value,yld,side,face,cur) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", blocks)


def test_merge_dedupes_by_tradeno(svc):
    pdb, tape = svc
    d = date.today().isoformat()
    A, B = "RU000A0000A1", "RU000A0000B2"
    _seed(pdb,
          # 1 — мелкая (есть только в тиках), 2 — крупная, она же в block_trade
          ticks=[(A, 1, f"{d} 10:00:00", 100.0, 10, 10_000, "buy", "TQCB"),
                 (A, 2, f"{d} 10:05:00", 100.5, 5000, 5_025_000, "sell", "TQCB")],
          # 2 — та же сделка из ISS, 3 — адресная, которой в тиках нет в принципе
          blocks=[(2, A, A, f"{d} 10:05:00", "bonds", "TQCB", 100.5, 5000,
                   5_025_000, 14.2, "sell", 1000, "SUR"),
                  (3, B, B, f"{d} 10:07:00", "ndm", "PTOB", 99.0, 100000,
                   99_000_000, 15.0, None, 1000, "SUR")])

    rows = tape.read_tape(frm=d)
    assert [r["trade_id"] for r in rows] == [3, 2, 1]        # новые сверху
    assert tape.tape_stats(frm=d)["n"] == 3                  # без задвоения

    # у общей сделки побеждает block_trade: он богаче полями
    common = next(r for r in rows if r["trade_id"] == 2)
    assert common["yld"] == 14.2 and common["market"] == "bonds"

    # адресная опознана и не теряет режим
    ndm = next(r for r in rows if r["trade_id"] == 3)
    assert ndm["negotiated"] is True and ndm["board"] == "PTOB"


def test_filters(svc):
    pdb, tape = svc
    d = date.today().isoformat()
    A, B = "RU000A0000A1", "RU000A0000B2"
    _seed(pdb,
          ticks=[(A, 1, f"{d} 10:00:00", 100.0, 10, 10_000, "buy", "TQCB")],
          blocks=[(2, A, A, f"{d} 10:05:00", "bonds", "TQCB", 100.5, 5000,
                   5_025_000, 14.2, "sell", 1000, "SUR"),
                  (3, B, B, f"{d} 10:07:00", "ndm", "PTOB", 99.0, 100000,
                   99_000_000, 15.0, None, 1000, "SUR")])

    assert len(tape.read_tape(frm=d, min_value=1_000_000)) == 2
    assert len(tape.read_tape(frm=d, side="buy")) == 1
    assert len(tape.read_tape(frm=d, isins=[A])) == 2
    assert len(tape.read_tape(frm=d, boards=["PTOB"])) == 1
    # адресный режим: тики в выборку не идут вообще
    ndm = tape.read_tape(frm=d, market="ndm")
    assert [r["trade_id"] for r in ndm] == [3]
    # безадресный: адресная сделка отсекается, тик остаётся
    assert sorted(r["trade_id"] for r in tape.read_tape(frm=d, market="bonds")) == [1, 2]


def test_big_isin_list_goes_to_temp_table(svc):
    """Список бумаг длиннее лимита переменных SQLite не должен ломать запрос —
    раньше такой фильтр приходилось молча выключать."""
    pdb, tape = svc
    d = date.today().isoformat()
    A = "RU000A0000A1"
    _seed(pdb,
          ticks=[(A, 1, f"{d} 10:00:00", 100.0, 10, 10_000, "buy", "TQCB")],
          blocks=[(2, A, A, f"{d} 10:05:00", "bonds", "TQCB", 100.5, 5000,
                   5_025_000, 14.2, "sell", 1000, "SUR")])
    many = [f"RU000FAKE{i:04d}" for i in range(1500)] + [A]
    assert len(tape.read_tape(frm=d, isins=many)) == 2
    assert tape.tape_stats(frm=d, isins=many)["n"] == 2
