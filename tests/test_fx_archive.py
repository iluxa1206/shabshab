"""Архив курсов по дням (таблица fx_rate) и объём тика по курсу ДНЯ СДЕЛКИ.

Зачем архив: цена облигации — процент от номинала, а у замещающих выпусков
номинал в валюте. Рублёвый объём сделки за прошлую дату обязан считаться курсом
того дня — USD 03.08.2026 стоил 80,24 против 85,84 на 31.08, и единый
сегодняшний курс уводил объём всего окна на движение валюты.
"""
import importlib

import pytest


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.fx as mod
    importlib.reload(mod)
    return mod


def test_save_and_read_rate_on_date(fx):
    fx.save_rates("2026-08-03", {"USD": 80.24, "CNY": 11.2}, {"USD": "tom"})
    fx.save_rates("2026-08-31", {"USD": 85.84})
    assert fx.rate_on("USD", "2026-08-03") == pytest.approx(80.24)
    assert fx.rate_on("USD", "2026-08-31") == pytest.approx(85.84)
    # рубль всегда единица, в архив не пишется
    assert fx.rate_on("RUB", "2026-08-03") == 1.0
    assert fx.rate_on("EUR", "2026-08-03") is None


def test_rate_on_stretches_last_known(fx):
    """Выходных в архиве нет — курс субботы это курс пятницы, а не пусто."""
    fx.save_rates("2026-08-07", {"USD": 82.39})
    assert fx.rate_on("USD", "2026-08-09") == pytest.approx(82.39)


def test_last_value_of_day_wins(fx):
    """Курс TOM ходит внутри дня: «курс дня» — последний известный уровень."""
    fx.save_rates("2026-08-03", {"USD": 80.0})
    fx.save_rates("2026-08-03", {"USD": 80.9})
    assert fx.rate_on("USD", "2026-08-03") == pytest.approx(80.9)


def test_rates_by_day_carries_edge_value(fx):
    """Карта окна несёт и последнее значение ДО окна: сделка в первый день
    окна считается курсом предыдущего торгового дня, а не теряет его."""
    fx.save_rates("2026-07-31", {"USD": 79.0})
    fx.save_rates("2026-08-05", {"USD": 80.82})
    m = fx.rates_by_day("USD", "2026-08-01", "2026-08-10")
    assert m["2026-08-05"] == pytest.approx(80.82)
    assert m["2026-07-31"] == pytest.approx(79.0)


@pytest.mark.asyncio
async def test_remember_debounced(fx, monkeypatch):
    """get_fx зовётся десятками раз в минуту — архив пишем реже. Сама запись
    уходит в поток: SQLite синхронный, а базу непрерывно пишет поток тиков."""
    calls = []
    monkeypatch.setattr(fx, "save_rates",
                        lambda day, rates, source=None: calls.append(day))
    await fx._remember({"USD": 85.0}, {})
    await fx._remember({"USD": 85.1}, {})
    assert len(calls) == 1


def test_tick_value_uses_rate_of_trade_day(fx):
    """Объём тика считается курсом дня сделки, а не последним известным."""
    import services.trades_archive as ta
    importlib.reload(ta)
    raw = [{"id": 1, "price": 100.0, "qty": 1, "time": "2026-08-03T09:00:00Z"},
           {"id": 2, "price": 100.0, "qty": 1, "time": "2026-08-31T09:00:00Z"}]
    rows = ta._tick_rows("RU000USD001", raw, {}, 1000.0,
                         {"2026-08-03": 80.24, "2026-08-31": 85.84})
    assert [r[5] for r in rows] == [pytest.approx(80240.0), pytest.approx(85840.0)]


def test_tick_value_day_missing_takes_previous(fx):
    """Дня нет в карте курсов (выходной) — берётся предыдущий известный."""
    import services.trades_archive as ta
    importlib.reload(ta)
    raw = [{"id": 3, "price": 100.0, "qty": 1, "time": "2026-08-09T09:00:00Z"}]
    rows = ta._tick_rows("RU000USD001", raw, {}, 1000.0, {"2026-08-07": 82.39})
    assert rows[0][5] == pytest.approx(82390.0)


@pytest.mark.asyncio
async def test_repair_fx_values_recomputes_only_unmatched(fx, monkeypatch):
    """Пересчёт по курсу дня трогает только тики БЕЗ биржевого двойника:
    где двойник есть, объём ставится по VALUE самой биржи (repair_values),
    и наш пересчёт был бы шагом назад."""
    import services.trades_archive as ta
    importlib.reload(ta)
    from datetime import date, timedelta
    day = (date.today() - timedelta(days=1)).isoformat()

    with ta._lock, ta._connect() as c:
        for tid in (1, 2):
            c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,"
                      "board) VALUES('RU000USD001',?,?,100.0,1,85840.0,'buy','TQCB')",
                      (tid, f"{day} 12:00:00"))
        # у второго тика есть строка ISS — её не трогаем
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,cur) VALUES(2,'RU000USD001','RU000USD001',?,'bonds',"
                  "'TQCB',100.0,1,80240.0,'SUR')", (f"{day} 12:00:00",))
    fx.save_rates(day, {"USD": 80.24})

    async def _units():
        return {"RU000USD001": "USD"}

    async def _faces(client, secid, board, frm, till):
        return {day: 1000.0}

    async def _resolve(isin, board):
        return isin, "TQCB"

    monkeypatch.setattr(ta, "face_units", _units)
    monkeypatch.setattr("services.bars.fetch_daily_face", _faces)
    monkeypatch.setattr("services.backdate.resolve_market", _resolve)

    res = await ta.repair_fx_values(days=3)
    assert res["rows"] == 1
    with ta._connect() as c:
        vals = dict(c.execute("SELECT trade_id, value FROM trade_tick"))
    assert vals[1] == pytest.approx(80240.0)     # пересчитан курсом дня
    assert vals[2] == pytest.approx(85840.0)     # оставлен под repair_values


@pytest.mark.asyncio
async def test_iss_value_of_currency_board_converted_to_rubles(fx, monkeypatch):
    """Сделка с юаневого борда: ISS отдаёт VALUE в юанях, в базу кладём рубли.

    До фикса такая строка лежала с cur='SUR' (валюта бралась по бумаге, а
    бумага торгуется и на рублёвом борде) — её объём попадал в рублёвые итоги
    заниженным в ~12,7 раза и через сверку затягивался в тиковый архив."""
    import importlib
    import services.block_trades as bt
    importlib.reload(bt)
    fx.save_rates("2026-08-31", {"CNY": 12.8})
    secmap = {"RU000A10FAK6": {"isin": "RU000A10FAK6", "face": 10000.0, "cur": "SUR"}}
    rows = [{"TRADENO": 1, "SECID": "RU000A10FAK6", "BOARDID": "TQOY",
             "TRADEDATE": "2026-08-31", "TRADETIME": "12:00:00",
             "PRICE": 93.48, "QUANTITY": 3, "VALUE": 28044.0, "BUYSELL": "B"},
            {"TRADENO": 2, "SECID": "RU000A10FAK6", "BOARDID": "TQOB",
             "TRADEDATE": "2026-08-31", "TRADETIME": "12:00:00",
             "PRICE": 93.48, "QUANTITY": 3, "VALUE": 358963.2, "BUYSELL": "B"}]
    n, _ = bt.upsert_trades(rows, "bonds", secmap, {"TQOY": "CNY"})
    assert n == 2
    with bt._connect() as c:
        got = {r["trade_id"]: (r["value"], r["cur"])
               for r in c.execute("SELECT trade_id, value, cur FROM block_trade")}
    assert got[1] == (pytest.approx(28044.0 * 12.8), "SUR")   # юани → рубли
    assert got[2] == (pytest.approx(358963.2), "SUR")         # рублёвый борд как был


@pytest.mark.asyncio
async def test_repair_currency_values_fixes_history(fx, monkeypatch):
    """Уже записанные валютные суммы приводятся к рублям по курсу дня."""
    import importlib
    import services.block_trades as bt
    importlib.reload(bt)
    fx.save_rates("2026-08-20", {"CNY": 12.6})

    async def _boards(client=None, force=False):
        return {"TQOY": "CNY"}

    monkeypatch.setattr(bt, "board_ccy_map", _boards)
    with bt._lock, bt._connect() as c:
        # 1 — в юанях (как приходило от ISS), 2 — уже в рублях, трогать нельзя
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,face,cur) VALUES(1,'RU000A10FAK6','RU000A10FAK6',"
                  "'2026-08-20 12:00:00','bonds','TQOY',100.0,1,10000.0,10000.0,'SUR')")
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,face,cur) VALUES(2,'RU000A10FAK6','RU000A10FAK6',"
                  "'2026-08-20 12:00:00','bonds','TQOY',100.0,1,126000.0,10000.0,'SUR')")
    res = await bt.repair_currency_values(days=3650)
    assert res["rows"] == 1
    with bt._connect() as c:
        vals = dict(c.execute("SELECT trade_id, value FROM block_trade"))
    assert vals[1] == pytest.approx(126000.0)
    assert vals[2] == pytest.approx(126000.0)


@pytest.mark.asyncio
async def test_repair_fixes_stale_face_of_rouble_bond(fx, monkeypatch):
    """Рублёвая амортизируемая бумага: живой поток записал объём по старому
    номиналу (500 при биржевых 250) — пересчёт по номиналу дня это чинит."""
    import importlib
    import services.trades_archive as ta
    importlib.reload(ta)
    from datetime import date, timedelta
    day = (date.today() - timedelta(days=1)).isoformat()
    with ta._lock, ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES('RU000RUB001',1,?,100.0,10,5000.0,'buy','TQCB')",
                  (f"{day} 12:00:00",))

    async def _units():
        return {"RU000RUB001": "SUR"}

    async def _faces(client, secid, board, frm, till):
        return {day: 250.0}

    async def _resolve(isin, board):
        return isin, "TQCB"

    monkeypatch.setattr(ta, "face_units", _units)
    monkeypatch.setattr("services.bars.fetch_daily_face", _faces)
    monkeypatch.setattr("services.backdate.resolve_market", _resolve)

    res = await ta.repair_fx_values(days=3)
    assert res["rows"] == 1
    with ta._connect() as c:
        assert c.execute("SELECT value FROM trade_tick").fetchone()[0] == pytest.approx(2500.0)


@pytest.mark.asyncio
async def test_only_currency_skips_rouble_bonds(fx, monkeypatch):
    """Режим «только валютные» рублёвые выпуски не трогает."""
    import importlib
    import services.trades_archive as ta
    importlib.reload(ta)
    from datetime import date, timedelta
    day = (date.today() - timedelta(days=1)).isoformat()
    with ta._lock, ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES('RU000RUB001',2,?,100.0,10,5000.0,'buy','TQCB')",
                  (f"{day} 12:00:00",))

    async def _units():
        return {"RU000RUB001": "SUR"}

    monkeypatch.setattr(ta, "face_units", _units)
    res = await ta.repair_fx_values(days=3, only_currency=True)
    assert res["rows"] == 0
