"""Живая лента сделок из Alor WS: буфер пушей → trade_tick.

Проверяем ровно то, что ломается молча: рублёвый объём считается по номиналу
бумаги (у амортизируемых он не 1000), повторный пуш той же сделки не плодит
дублей (ISS-копия того же TRADENO доедет позже своей лентой), а порог для бумаг
ВНЕ флоатер-юниверса не задевает сам юниверс — по нему тик кормит бары и VWAP.
"""
import importlib

import pytest


@pytest.fixture()
def ts(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.trades_archive as ta
    importlib.reload(ta)
    import services.trades_stream as mod
    importlib.reload(mod)
    mod._core.add("RU000TEST01")        # бумага юниверса: пишем любой размер
    return pdb, mod


def _push(mod, tid, price=100.0, qty=10):
    mod._on_trade("RU000TEST01", {"id": tid, "price": price, "qty": qty,
                                  "time": "2026-08-12T09:15:00Z", "side": "buy",
                                  "board": "TQCB"})


def test_push_writes_tick_with_face(ts):
    pdb, mod = ts
    _push(mod, 1, price=99.0, qty=25)
    chunks = [(i, r) for i, r in mod._buf.items()]
    assert mod._flush_sync(chunks, {"RU000TEST01": 800.0}) == 1
    with pdb._connect() as c:
        r = dict(c.execute("SELECT * FROM trade_tick").fetchone())
    assert r["ts"] == "2026-08-12 12:15:00"          # UTC пуша → МСК архива
    assert r["side"] == "buy" and r["board"] == "TQCB"
    # 25 бумаг × 800 ₽ × 99% — номинал берётся амортизированный, не дефолтный
    assert r["value"] == pytest.approx(19800.0)


def test_same_trade_twice_is_one_row(ts):
    """Пуш повторился (реконнект) — INSERT OR IGNORE по (isin, trade_id)."""
    pdb, mod = ts
    _push(mod, 7)
    assert mod._flush_sync(list(mod._buf.items()), {}) == 1
    mod._buf.clear()
    _push(mod, 7)
    assert mod._flush_sync(list(mod._buf.items()), {}) == 0
    with pdb._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM trade_tick").fetchone()[0] == 1


def test_broken_push_ignored(ts):
    """Пуш без цены/объёма в архив не попадает и буфер не засоряет."""
    pdb, mod = ts
    mod._on_trade("RU000TEST01", {"id": 1, "price": None, "qty": 5})
    mod._on_trade("RU000TEST01", {"id": 2, "price": 100.0, "qty": 0})
    assert mod._buf == {}


def test_small_trade_outside_universe_skipped(ts):
    """Вне юниверса пишем от порога: мелочь по ОФЗ-ПД в ленте не показывается
    (её нижняя планка 1 млн ₽), а архив от неё растёт кратно."""
    _pdb, mod = ts
    mod._faces["map"] = {"RU000OTHER1": 1000.0}   # номинал известен — судим порогом
    mod._on_trade("RU000OTHER1", {"id": 1, "price": 100.0, "qty": 10,
                                  "time": "2026-08-12T09:15:00Z", "board": "TQOB"})
    assert mod._buf == {} and mod._stats["skipped_small"] == 1


def test_big_trade_outside_universe_kept(ts):
    """Крупная сделка вне юниверса — ради неё стрим и расширяли: ISS отдал бы её
    через 15 минут."""
    _pdb, mod = ts
    mod._on_trade("RU000OTHER1", {"id": 2, "price": 100.0, "qty": 2000,
                                  "time": "2026-08-12T09:15:00Z", "board": "TQOB"})
    assert len(mod._buf.get("RU000OTHER1") or []) == 1


def test_small_trade_inside_universe_kept(ts):
    """Порог не касается юниверса: тик там — источник часовых баров и VWAP."""
    _pdb, mod = ts
    mod._on_trade("RU000TEST01", {"id": 3, "price": 100.0, "qty": 1,
                                  "time": "2026-08-12T09:15:00Z", "board": "TQCB"})
    assert len(mod._buf.get("RU000TEST01") or []) == 1


def test_fixed_tick_goes_to_tape_and_live_count(ts, monkeypatch):
    """ФИКС: мелкая сделка идёт и в живой счёт дня, и в ленту — порога для него
    нет, как и для флоатер-юниверса.

    Порог существует ради бумаг вне обеих витрин. У фикса своя лента, и сделка
    должна быть видна сразу, а не через час плановым добором. Объём базы от этого
    не растёт: те же тики доезжают добором, а вставка идёт INSERT OR IGNORE по
    (isin, trade_id)."""
    _pdb, mod = ts
    from services import live_quotes
    seen = []
    # набор фиксов подменяем, а не дополняем: он живёт на модуле и переживает
    # тест (guard «нет свежего универса — держим прежний»)
    monkeypatch.setattr(mod, "_fixed", {"RU000FIXED1"})
    monkeypatch.setattr(live_quotes, "add_trade",
                        lambda isin, *a, **kw: seen.append(isin))
    mod._on_trade("RU000FIXED1", {"id": 7, "price": 100.0, "qty": 5,
                                  "time": "2026-08-12T09:15:00Z", "board": "TQCB"})
    assert seen == ["RU000FIXED1"], "живой счёт дня получил тик"
    assert len(mod._buf.get("RU000FIXED1") or []) == 1, "тик уехал в ленту/архив"
    assert mod._stats["skipped_small"] == 0


def test_currency_face_value_in_rubles(ts):
    """Замещайка: номинал в долларах, объём в архиве — рублёвый.

    До фикса 2026-08-31 в trade_tick ложилось qty*face*price/100 БЕЗ курса, и
    сделка на ~10 млн ₽ лежала как 118 тыс. — мимо фильтра ленты «от 1 млн» и
    мимо порога записи потока."""
    pdb, mod = ts
    mod._faces["unit"] = {"RU000TEST01": "USD"}
    mod._fx["rates"] = {"USD": 80.0}
    _push(mod, 11, price=100.0, qty=2)
    fx = mod._fx_for(["RU000TEST01"])
    assert mod._flush_sync(list(mod._buf.items()), {"RU000TEST01": 1000.0}, fx) == 1
    with pdb._connect() as c:
        val = c.execute("SELECT value FROM trade_tick").fetchone()[0]
    assert val == pytest.approx(2 * 1000.0 * 80.0)      # 2 бумаги по 1000 USD


def test_no_face_in_map_is_not_cut_by_threshold(ts):
    """Бумаги нет в карте номиналов — порог по ней не судим.

    Считать по 1000 ₽ для баров можно, а вот резать таким объёмом запись нельзя:
    выпуск с номиналом 200 000 ₽ терял бы сделки на десятки миллионов."""
    _pdb, mod = ts
    mod._faces["map"] = {}
    mod._on_trade("RU000OTHER2", {"id": 12, "price": 100.0, "qty": 3,
                                  "time": "2026-08-12T09:15:00Z", "board": "TQCB"})
    assert len(mod._buf.get("RU000OTHER2") or []) == 1
    assert mod._stats["skipped_small"] == 0


def test_known_small_trade_outside_universe_still_cut(ts):
    """Номинал известен и объём вправду мелкий — порог работает как раньше."""
    _pdb, mod = ts
    mod._faces["map"] = {"RU000OTHER3": 1000.0}
    mod._on_trade("RU000OTHER3", {"id": 13, "price": 100.0, "qty": 3,
                                  "time": "2026-08-12T09:15:00Z", "board": "TQCB"})
    assert not mod._buf.get("RU000OTHER3")
    assert mod._stats["skipped_small"] == 1
