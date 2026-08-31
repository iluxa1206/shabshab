"""Рублёвый объём тика: номинал по дню + валюта номинала.

Цена у Alor — процент от НОМИНАЛА. У амортизируемых он меняется по дням, у
замещающих и юаневых выражен в валюте (FACEUNIT) при рублёвых расчётах. До фикса
2026-08-31 объём считался без курса, и сделка на 10 млн ₽ лежала в архиве как
118 тыс. — мимо фильтров ленты и мимо порога записи потока.
"""
import importlib

import pytest


@pytest.fixture()
def ta(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.trades_archive as mod
    importlib.reload(mod)
    return pdb, mod


def _raw(tid=1, price=100.0, qty=2, day="2026-08-12"):
    return [{"id": tid, "price": price, "qty": qty,
             "time": f"{day}T09:15:00Z", "side": "buy", "board": "TQCB"}]


def test_rouble_bond_value_unchanged(ta):
    """Рублёвая бумага: множитель 1.0, поведение прежнее."""
    _pdb, mod = ta
    rows = mod._tick_rows("RU000TEST01", _raw(qty=3), {"2026-08-12": 800.0})
    assert rows[0][5] == pytest.approx(3 * 800.0)      # 3 бумаги × 800 ₽ × 100%


def test_currency_face_multiplied_by_rate(ta):
    """Номинал в долларах — объём в рублях по курсу."""
    _pdb, mod = ta
    rows = mod._tick_rows("RU000USD001", _raw(qty=2), {"2026-08-12": 1000.0},
                          fx_rate=85.6)
    assert rows[0][5] == pytest.approx(2 * 1000.0 * 85.6)


def test_face_taken_from_trade_day(ta):
    """Номинал берётся на ДЕНЬ СДЕЛКИ: после амортизации он другой."""
    _pdb, mod = ta
    faces = {"2026-08-10": 1000.0, "2026-08-12": 400.0}
    rows = mod._tick_rows("RU000AMORT1", _raw(qty=1, day="2026-08-12"), faces)
    assert rows[0][5] == pytest.approx(400.0)


def test_bulk_applies_per_isin_rate(ta):
    """Пачка стрима: курс на бумагу, рублёвые в карте курсов отсутствуют."""
    pdb, mod = ta
    chunks = [("RU000USD001", _raw(tid=5)), ("RU000TEST01", _raw(tid=6))]
    n = mod.upsert_ticks_bulk(chunks, {"RU000USD001": 1000.0, "RU000TEST01": 1000.0},
                              {"RU000USD001": 85.6})
    assert n == 2
    with pdb._connect() as c:
        vals = dict(c.execute("SELECT isin, value FROM trade_tick"))
    assert vals["RU000USD001"] == pytest.approx(2 * 1000.0 * 85.6)
    assert vals["RU000TEST01"] == pytest.approx(2 * 1000.0)


@pytest.mark.asyncio
async def test_face_fx_unknown_rate_falls_back_to_one(ta, monkeypatch):
    """Курса нет — множитель 1.0: объём занижен, но сделка в архиве есть."""
    _pdb, mod = ta
    monkeypatch.setattr(mod, "_units", {"at": 9e18, "map": {"RU000XXX001": "USD"}})
    monkeypatch.setattr(mod, "_fx", {"at": 9e18, "rates": {"CNY": 11.5}})
    assert await mod.face_fx("RU000XXX001") == 1.0


@pytest.mark.asyncio
async def test_face_fx_reads_rate_by_face_unit(ta, monkeypatch):
    _pdb, mod = ta
    monkeypatch.setattr(mod, "_units", {"at": 9e18, "map": {"RU000CNY001": "CNH",
                                                            "RU000RUB001": "SUR"}})
    monkeypatch.setattr(mod, "_fx", {"at": 9e18, "rates": {"CNY": 11.5}})
    assert await mod.face_fx("RU000CNY001") == pytest.approx(11.5)
    assert await mod.face_fx("RU000RUB001") == 1.0


def test_repair_values_takes_iss_truth(ta):
    """Уже записанный тик с промахом по номиналу чинится из block_trade."""
    pdb, mod = ta
    with pdb._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board)"
                  " VALUES('RU000USD001',1,'2026-08-12 12:15:00',100.0,2,2000.0,"
                  "'buy','TQCB')")
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,cur) VALUES(1,'RU000USD001','RU000USD001',"
                  "'2026-08-12 12:15:00','bonds','TQCB',100.0,2,171202.0,'SUR')")
    res = mod.repair_values(days=0, since="2026-08-01")
    assert res["rows"] == 1 and res["delta"] == pytest.approx(169202.0)
    with pdb._connect() as c:
        assert c.execute("SELECT value FROM trade_tick").fetchone()[0] == 171202.0


def test_repair_skips_currency_settlement(ta):
    """У валютных РАСЧЁТОВ биржевой VALUE не в рублях — такой строкой не чиним."""
    pdb, mod = ta
    with pdb._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board)"
                  " VALUES('RU000CNY001',2,'2026-08-12 12:15:00',100.0,2,23000.0,"
                  "'buy','TQCB')")
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,cur) VALUES(2,'RU000CNY001','RU000CNY001',"
                  "'2026-08-12 12:15:00','bonds','TQCB',100.0,2,2000.0,'CNY')")
    assert mod.repair_values(days=0, since="2026-08-01")["rows"] == 0
    with pdb._connect() as c:
        assert c.execute("SELECT value FROM trade_tick").fetchone()[0] == 23000.0


def test_repair_dry_run_changes_nothing(ta):
    pdb, mod = ta
    with pdb._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board)"
                  " VALUES('RU000USD001',3,'2026-08-12 12:15:00',100.0,2,2000.0,"
                  "'buy','TQCB')")
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,cur) VALUES(3,'RU000USD001','RU000USD001',"
                  "'2026-08-12 12:15:00','bonds','TQCB',100.0,2,171202.0,'SUR')")
    assert mod.repair_values(days=0, since="2026-08-01", dry_run=True)["rows"] == 1
    with pdb._connect() as c:
        assert c.execute("SELECT value FROM trade_tick").fetchone()[0] == 2000.0
