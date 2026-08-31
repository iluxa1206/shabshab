"""Дневные итоги безадресных торгов (bond_day) — полный оборот рынка.

Поштучно живой поток пишет всё только по бумагам витрин; вне их — от порога
TRADES_STREAM_MIN_RUB, и оборот таких выпусков в ленте занижен (замер
2026-08-28: 20,6 против 22,4 млрд ₽, у RU000A10FNA0 0,2 млн вместо 46,3). Этот
слой добирает недостающее дневным итогом биржи, не раздувая архив сделок.
"""
import importlib

import pytest


@pytest.fixture()
def bt(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.fx as fx
    importlib.reload(fx)
    import services.block_trades as mod
    importlib.reload(mod)
    import services.tape as tape
    importlib.reload(tape)
    return mod, tape, fx


SECMAP = {"RU000A0000A1": {"isin": "RU000A0000A1", "face": 1000.0},
          "RU000A0000C3": {"isin": "RU000A0000C3", "face": 10000.0}}


def _row(secid, board, value, date="2026-08-28", **kw):
    r = {"SECID": secid, "BOARDID": board, "TRADEDATE": date, "VALUE": value,
         "NUMTRADES": 10, "VOLUME": 5, "WAPRICE": 100.0, "CLOSE": 100.1}
    r.update(kw)
    return r


def test_rouble_board_saved_as_is(bt):
    mod, _tape, _fx = bt
    assert mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 5_000_000.0)],
                                SECMAP, {}, {}) == 1
    with mod._connect() as c:
        r = dict(c.execute("SELECT * FROM bond_day").fetchone())
    assert r["value"] == pytest.approx(5_000_000.0) and r["cur"] == "SUR"


def test_currency_board_converted_by_day_rate(bt):
    """Юаневый борд: биржа отдаёт объём в юанях — храним рубли по курсу дня."""
    mod, _tape, _fx = bt
    rows = [_row("RU000A0000C3", "TQOY", 100_000.0)]
    assert mod.upsert_bond_days(rows, SECMAP, {"TQOY": "CNY"},
                                {("CNY", "2026-08-28"): 12.8}) == 1
    with mod._connect() as c:
        r = dict(c.execute("SELECT value, cur FROM bond_day").fetchone())
    assert r["value"] == pytest.approx(1_280_000.0) and r["cur"] == "CNY"


def test_currency_board_without_rate_skipped(bt):
    """Курса дня нет — строку не пишем: юани в рублёвой колонке хуже, чем дыра."""
    mod, _tape, _fx = bt
    assert mod.upsert_bond_days([_row("RU000A0000C3", "TQOY", 100_000.0)],
                                SECMAP, {"TQOY": "CNY"}, {}) == 0


def test_unknown_secid_skipped(bt):
    """В ленте рынка живут не только облигации — чужое не пишем."""
    mod, _tape, _fx = bt
    assert mod.upsert_bond_days([_row("SBER", "TQBR", 9e9)], SECMAP, {}, {}) == 0


def test_reimport_updates_not_duplicates(bt):
    """Повторный проход даты обновляет строку, а не плодит вторую."""
    mod, _tape, _fx = bt
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 1_000_000.0)], SECMAP, {}, {})
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 3_000_000.0)], SECMAP, {}, {})
    with mod._connect() as c:
        rows = c.execute("SELECT value FROM bond_day").fetchall()
    assert len(rows) == 1 and rows[0]["value"] == pytest.approx(3_000_000.0)


def test_market_turnover_sums_boards_and_days(bt):
    """Оборот окна — сумма по бордам и дням выбранных бумаг."""
    mod, tape, _fx = bt
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 2e6),
                          _row("RU000A0000A1", "TQOB", 1e6),
                          _row("RU000A0000A1", "TQCB", 4e6, date="2026-08-27"),
                          _row("RU000A0000C3", "TQCB", 9e6)], SECMAP, {}, {})
    all_win = tape.market_turnover("2026-08-27", "2026-08-28")
    assert all_win["value"] == pytest.approx(16e6) and all_win["isins"] == 2
    one = tape.market_turnover("2026-08-28", "2026-08-28", ["RU000A0000A1"])
    assert one["value"] == pytest.approx(3e6)


def test_tape_stats_reports_market_value(bt):
    """Итоги ленты несут биржевой оборот тех же бумаг…"""
    mod, tape, _fx = bt
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 46_300_000.0)], SECMAP, {}, {})
    with mod._lock, mod._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES('RU000A0000A1',1,'2026-08-28 12:00:00',100.0,1,200000.0,"
                  "'buy','TQCB')")
    st = tape.tape_stats(frm="2026-08-28", till="2026-08-28")
    assert st["value"] == pytest.approx(200_000.0)
    assert st["market_value"] == pytest.approx(46_300_000.0)


def test_market_value_survives_trade_filters(bt):
    """…в том числе под порогом суммы: он фильтрует СДЕЛКИ, а набор бумаг задают
    охват и признаки выпуска. Умолчание вкладки — «от 10 млн», и обнуляться на
    нём показатель не должен: он читается как «крупные сделки на X из Y оборота
    этих бумаг»."""
    mod, tape, _fx = bt
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 46_300_000.0)], SECMAP, {}, {})
    assert tape.tape_stats(frm="2026-08-28", min_value=1e7)["market_value"] \
        == pytest.approx(46_300_000.0)
    assert tape.tape_stats(frm="2026-08-28", side="buy")["market_value"] \
        == pytest.approx(46_300_000.0)


def test_market_value_follows_selected_market(bt):
    """Показатель считает ТЕ ЖЕ режимы, что показывает лента: безадресные из
    bond_day, адресные из block_day. Иначе число рядом с оборотом сравнивалось
    бы с другим множеством сделок — у флоатеров за 28.08 адресных 137 млрд
    против 7,7 безадресных."""
    mod, tape, _fx = bt
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 46_300_000.0)], SECMAP, {}, {})
    with mod._lock, mod._connect() as c:
        c.execute("INSERT INTO block_day(isin,date,board,secid,numtrades,value) "
                  "VALUES('RU000A0000A1','2026-08-28','PSOB','RU000A0000A1',2,"
                  "137_000_000.0)")
    assert tape.tape_stats(frm="2026-08-28", market="bonds")["market_value"] \
        == pytest.approx(46_300_000.0)
    assert tape.tape_stats(frm="2026-08-28", market="ndm")["market_value"] \
        == pytest.approx(137_000_000.0)
    # режим не выбран — лента показывает и то, и другое
    assert tape.tape_stats(frm="2026-08-28")["market_value"] \
        == pytest.approx(183_300_000.0)


def test_traded_boards_from_history(bt):
    """Список бордов для снимка текущего дня берётся из уже собранной истории:
    market-level marketdata отдаёт бумагу только на основном режиме, поэтому
    снимок идёт по бордам, и список обязан соответствовать рынку."""
    mod, _tape, _fx = bt
    from datetime import date, timedelta
    day = (date.today() - timedelta(days=1)).isoformat()
    assert mod._traded_boards() == ["TQCB", "TQOB", "TQIR", "TQRD", "TQOY",
                                    "TQOD", "TQOE"]     # пусто → умолчание
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 1e6, date=day),
                          _row("RU000A0000C3", "TQOB", 1e6, date=day)],
                         SECMAP, {}, {})
    assert mod._traded_boards() == ["TQCB", "TQOB"]


def test_traded_boards_ignores_stale_dates(bt):
    """Борд, на котором неделю не торговали, из списка выпадает."""
    mod, _tape, _fx = bt
    mod.upsert_bond_days([_row("RU000A0000A1", "TQCB", 1e6, date="2020-01-01")],
                         SECMAP, {}, {})
    assert "TQCB" in mod._traded_boards()      # умолчание, история старая
    assert len(mod._traded_boards()) == 7


@pytest.mark.asyncio
async def test_negotiated_currency_board_converted(bt, monkeypatch):
    """Адресный юаневый борд: дневной агрегат тоже приводится к рублям."""
    mod, _tape, fx = bt
    fx.save_rates("2026-08-28", {"CNY": 12.8})
    rows = [_row("RU000A0000C3", "PTOY", 100_000.0)]
    assert mod.upsert_days(rows, SECMAP, {"PTOY": "CNY"},
                           {("CNY", "2026-08-28"): 12.8}) == 1
    with mod._connect() as c:
        assert c.execute("SELECT value FROM block_day").fetchone()[0] \
            == pytest.approx(1_280_000.0)


@pytest.mark.asyncio
async def test_repair_currency_days_fixes_history(bt, monkeypatch):
    """Уже записанные валютные агрегаты РПС чинятся курсом дня."""
    mod, _tape, fx = bt
    fx.save_rates("2026-08-28", {"CNY": 12.8})

    async def _boards(client=None, force=False):
        return {"PTOY": "CNY"}

    monkeypatch.setattr(mod, "board_ccy_map", _boards)
    with mod._lock, mod._connect() as c:
        c.execute("INSERT INTO block_day(isin,date,board,secid,numtrades,value,"
                  "waprice,volume,face) VALUES('RU000A0000C3','2026-08-28','PTOY',"
                  "'RU000A0000C3',1,100000.0,100.0,10,10000.0)")
    res = await mod.repair_currency_days()
    assert res["rows"] == 1
    with mod._connect() as c:
        assert c.execute("SELECT value FROM block_day").fetchone()[0] \
            == pytest.approx(1_280_000.0)
