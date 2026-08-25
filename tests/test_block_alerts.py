"""Колокольчик по крупным сделкам: звонят только флоатеры.

Крупняк рынка в рублях — это почти целиком ОФЗ-ПД, поэтому без фильтра
уведомления вырождались в ленту фиксов. Отдельно проверяется, что водяной знак
двигается и по отфильтрованным сделкам: иначе пачка фиксов подряд встала бы
перед выборкой намертво и флоатер за ней не позвонил бы никогда.
"""
import asyncio
import importlib
import time
from datetime import date

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


def _seed(pdb, rows, ins_at=None):
    d = date.today().isoformat()
    now = int(time.time()) if ins_at is None else ins_at
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,qty,"
            "value,yld,side,face,cur,ins_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(tid, isin, isin, f"{d} 10:0{i}:00", "bonds", "TQCB", 100.0, 1000,
              val, None, "buy", 1000, "SUR", now)
             for i, (tid, isin, val) in enumerate(rows)])


def _queued(pdb):
    """Что осталось в очереди звонка."""
    with pdb._connect() as c:
        return [r[0] for r in c.execute(
            "SELECT trade_id FROM block_trade WHERE alerted=0 ORDER BY trade_id")]


def _run(mod, monkeypatch, sent):
    """Гоняет notify_blocks с замоканными пользователями и WS."""
    monkeypatch.setattr(mod, "_secmap", {"at": None, "map": {}})

    import services.instruments_registry as reg
    monkeypatch.setattr(reg, "labels_map", lambda: {
        "RU000FLOAT01": {"name": "Флоатер", "base": "KEYRATE"},
        "RU000FIXED01": {"name": "Фикс", "base": "FIXED"},
    })
    import services.auth_users as au
    monkeypatch.setattr(au, "list_users", lambda: [{"email": "u@x.ru"}])

    from api.routes import ws as wsmod

    async def _bc(user, payload):
        sent.append(payload)
    monkeypatch.setattr(wsmod.manager, "broadcast_signal", _bc)
    return asyncio.run(mod.notify_blocks())


def test_fixed_does_not_ring(bt, monkeypatch):
    pdb, mod = bt
    big = mod.BLOCK_ALERT_MIN_RUB
    _seed(pdb, [(1, "RU000FIXED01", big + 1), (2, "RU000FLOAT01", big + 1)])
    sent: list = []
    n = _run(mod, monkeypatch, sent)
    assert n == 1                                   # позвонил только флоатер
    isins = [m["isin"] for p in sent for m in p["matches"]]
    assert isins == ["RU000FLOAT01"]
    # с очереди сняты обе сделки — фикс больше не попадёт в выборку
    assert _queued(pdb) == []
    assert mod.pending_alerts() == []


def test_only_fixed_still_moves_watermark(bt, monkeypatch):
    """Пачка одних фиксов: звонков нет, но знак сдвинут — иначе залипание."""
    pdb, mod = bt
    big = mod.BLOCK_ALERT_MIN_RUB
    _seed(pdb, [(1, "RU000FIXED01", big + 1), (2, "RU000FIXED01", big + 2)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0
    assert sent == []
    assert _queued(pdb) == []


def test_below_threshold_never_rings(bt, monkeypatch):
    pdb, mod = bt
    _seed(pdb, [(1, "RU000FLOAT01", mod.BLOCK_ALERT_MIN_RUB - 1)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0


# ── фильтры пользователя (kind=block) вместо умолчания ──────────────────────

@pytest.fixture()
def sig(bt):
    """services.signals на той же временной базе, что и block_trades."""
    import importlib
    import services.signals as s
    return importlib.reload(s)


def test_user_filter_overrides_default(bt, sig, monkeypatch):
    """Свой фильтр важнее умолчания: порог ниже env-порога, база — фиксы."""
    pdb, mod = bt
    sig.create("u@x.ru", "мои фиксы",
               {"bases": ["FIXED"], "min_value_rub": 5_000_000}, kind="block")
    _seed(pdb, [(1, "RU000FIXED01", 6_000_000), (2, "RU000FLOAT01", 6_000_000)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 1
    assert [m["isin"] for p in sent for m in p["matches"]] == ["RU000FIXED01"]
    # имя фильтра доезжает до колокольчика — иначе «фильтр удалён» в ленте
    assert sent[0]["filter_name"] == "мои фиксы"
    assert sent[0]["filter_id"] > 0
    assert _queued(pdb) == []


def test_disabled_filter_does_not_fall_back_to_default(bt, sig, monkeypatch):
    """Выключенный фильтр — не «нет фильтров»: умолчание не воскресает."""
    pdb, mod = bt
    f = sig.create("u@x.ru", "тихий", {"min_value_rub": 5_000_000}, kind="block")
    sig.update("u@x.ru", f["id"], enabled=False)
    _seed(pdb, [(1, "RU000FLOAT01", mod.BLOCK_ALERT_MIN_RUB + 1)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0
    assert _queued(pdb) == []


def test_spread_filter_prices_trade_before_matching(bt, sig, monkeypatch):
    """Фильтр со спредом: y_idx досчитывается ДО отбора, иначе сделка терялась
    бы (непосчитанный спред такой фильтр трактует как «не подходит»)."""
    pdb, mod = bt
    sig.create("u@x.ru", "широкие блоки",
               {"min_value_rub": 5_000_000, "spread_min": 250}, kind="block")
    _seed(pdb, [(1, "RU000FLOAT01", 6_000_000)])

    from services import trade_yidx

    async def _for_rows(rows):
        for r in rows:
            r["y_idx_bps"] = 300.0
        return len(rows)
    monkeypatch.setattr(trade_yidx, "for_rows", _for_rows)

    sent: list = []
    assert _run(mod, monkeypatch, sent) == 1
    assert [m["val_bps"] for p in sent for m in p["matches"]] == [300.0]


def test_spread_filter_skips_trade_outside_range(bt, sig, monkeypatch):
    pdb, mod = bt
    sig.create("u@x.ru", "узкие блоки",
               {"min_value_rub": 5_000_000, "spread_max": 100}, kind="block")
    _seed(pdb, [(1, "RU000FLOAT01", 6_000_000)])

    from services import trade_yidx

    async def _for_rows(rows):
        for r in rows:
            r["y_idx_bps"] = 300.0
        return len(rows)
    monkeypatch.setattr(trade_yidx, "for_rows", _for_rows)

    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0
    assert _queued(pdb) == []                  # с очереди всё равно снята


# ── живой поток Alor вместо ISS с 15-минутным лагом ─────────────────────────

def test_stale_rows_never_ring(bt, monkeypatch):
    """Очередь ограничена окном по МОМЕНТУ ЗАПИСИ: рестарт процесса или
    перечитанная сессия не вываливают в колокольчик накопленное."""
    pdb, mod = bt
    old = int(time.time() - mod.ALERT_MAX_AGE_MIN * 60 - 60)
    _seed(pdb, [(1, "RU000FLOAT01", mod.BLOCK_ALERT_MIN_RUB + 1)], ins_at=old)
    assert mod.pending_alerts() == []
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0


def test_tick_ingest_rings_and_iss_copy_is_not_a_dupe(bt, monkeypatch):
    """Тик Alor звонит сразу; доехавшая позже ISS-копия той же сделки (тот же
    TRADENO) в очередь не возвращается."""
    pdb, mod = bt
    monkeypatch.setattr(mod, "_secmap", {"at": None, "map": {}})
    tick = {"isin": "RU000FLOAT01", "trade_id": 77,
            "ts": f"{date.today().isoformat()} 10:00:00", "price": 100.0,
            "qty": 1000, "value": mod.BLOCK_ALERT_MIN_RUB + 1, "side": "buy",
            "board": "TQCB"}
    assert mod.ingest_ticks([tick]) == 1
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 1
    assert [m["isin"] for p in sent for m in p["matches"]] == ["RU000FLOAT01"]

    # ISS приносит ту же сделку через 15 минут — INSERT OR IGNORE, звонка нет
    from services.block_trades import upsert_trades
    n, _unknown = upsert_trades(
        [{"TRADENO": 77, "SECID": "RU000FLOAT01", "VALUE": tick["value"],
          "PRICE": 100.0, "QUANTITY": 1000, "BOARDID": "TQCB", "BUYSELL": "B",
          "TRADEDATE": date.today().isoformat(), "TRADETIME": "10:00:00"}],
        "bonds", {"RU000FLOAT01": {"isin": "RU000FLOAT01", "face": 1000,
                                   "cur": "SUR"}})
    assert n == 0
    sent2: list = []
    assert _run(mod, monkeypatch, sent2) == 0


def _utc(minutes_ago: float) -> str:
    """Время тика в формате Alor (UTC, Z) — свежесть считается от него."""
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_stream_alert_rows_respect_floor():
    """В ленту блоков из потока уходит только то, что кому-то нужно."""
    from services.trades_stream import _alert_rows
    chunks = [("RU000FLOAT01", [
        {"id": 1, "price": 100.0, "qty": 10, "time": _utc(1),
         "side": "buy", "board": "TQCB", "val": 500_000},
        {"id": 2, "price": 100.0, "qty": 10_000, "time": _utc(1),
         "side": "sell", "board": "TQCB", "val": 5_000_000}])]
    rows = _alert_rows(chunks, 1_000_000)
    assert [r["trade_id"] for r in rows] == [2]
    assert rows[0]["isin"] == "RU000FLOAT01" and rows[0]["value"] == 5_000_000


def test_stale_tick_from_resubscribe_does_not_ring():
    """Переподписка добирает хвост у брокера: в архив он нужен, а звонить о
    сделке получасовой давности — нет."""
    from services.trades_stream import _alert_rows
    chunks = [("RU000FLOAT01", [
        {"id": 1, "price": 100.0, "qty": 10_000, "time": _utc(30),
         "side": "buy", "board": "TQCB", "val": 5_000_000},
        {"id": 2, "price": 100.0, "qty": 10_000, "time": _utc(1),
         "side": "buy", "board": "TQCB", "val": 5_000_000}])]
    assert [r["trade_id"] for r in _alert_rows(chunks, 1_000_000)] == [2]


def test_currency_face_tick_is_not_dropped_as_small():
    """Замещайка: номинал в долларах, порог — в рублях. Без курса объём
    занижался в 83 раза, и сделка на 8 млн ₽ выпадала как «мелочь»."""
    from services import trades_stream as ts
    ts._faces["map"]["RU000USD001"] = 100.0        # $100 номинал
    ts._faces["unit"]["RU000USD001"] = "USD"
    ts._fx["rates"] = {"USD": 80.0, "RUB": 1.0}
    val, fx_ok = ts._tick_value("RU000USD001", 100.0, 1000)
    assert fx_ok and val == 8_000_000              # 1000 × $100 × 100% × 80

    # курса нет — объём недостоверен: тик не режем порогом, но и не звоним
    ts._fx["rates"] = {"RUB": 1.0}
    val, fx_ok = ts._tick_value("RU000USD001", 100.0, 1000)
    assert not fx_ok and val == 100_000
    chunks = [("RU000USD001", [{"id": 1, "price": 100.0, "qty": 1000,
                                "time": _utc(1), "side": "buy", "board": "TQCB",
                                "val": val, "fx_ok": fx_ok}])]
    assert ts._alert_rows(chunks, 50_000) == []


def test_rouble_face_tick_unaffected_by_fx():
    """Рублёвая бумага считается как раньше — курс к ней не применяется."""
    from services import trades_stream as ts
    ts._faces["map"]["RU000RUB001"] = 1000.0
    ts._faces["unit"].pop("RU000RUB001", None)
    ts._fx["rates"] = {"USD": 80.0, "RUB": 1.0}
    assert ts._tick_value("RU000RUB001", 100.0, 1000) == (1_000_000.0, True)


def test_feed_min_value_has_tolerance(bt):
    """Лента и таблица живут по тому же люфту, что и фильтры: «от 5 млн»
    показывает сделку на 4,6 млн — иначе настройка на витрине и в алертах
    означала бы разное."""
    pdb, mod = bt
    _seed(pdb, [(1, "RU000FLOAT01", 4_600_000),
                (2, "RU000FLOAT01", 4_400_000)])
    got = {r["trade_id"] for r in mod.read_blocks(min_value=5_000_000)}
    assert got == {1}


# ── один принт в два канала ────────────────────────────────────────────────

def _targets(sig, monkeypatch, mapping):
    """Адресаты Telegram без реальной таблицы tg_targets: id → chat_id."""
    from services import tg_targets
    monkeypatch.setattr(tg_targets, "get", lambda tid: (
        {"user_email": "u@x.ru", "chat_id": mapping[int(tid)]}
        if int(tid) in mapping else None))


def test_one_trade_reaches_both_channels(bt, sig, monkeypatch):
    """Разные каналы — разные адресаты: широкий фильтр («Ф5», порог 1 млн) не
    имеет права забрать сделку у узкого («Р5», порог 50 млн), иначе канал Р5 не
    получает НИЧЕГО."""
    pdb, mod = bt
    _targets(sig, monkeypatch, {1: -100, 2: -200})
    sig.create("u@x.ru", "Ф5", {"min_value_rub": 1_000_000}, kind="block",
               tg_target_id=2)
    sig.create("u@x.ru", "Р5", {"min_value_rub": 50_000_000}, kind="block",
               tg_target_id=1)
    _seed(pdb, [(1, "RU000FLOAT01", 60_000_000)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 1        # сделка одна…
    # …а звонков по ней два — по одному на канал
    assert sorted(p["filter_name"] for p in sent) == ["Р5", "Ф5"]


def test_same_channel_still_gets_one_message(bt, sig, monkeypatch):
    """Два подходящих фильтра с одним адресатом — по-прежнему одно событие:
    дубль в тот же чат это шум."""
    pdb, mod = bt
    _targets(sig, monkeypatch, {1: -100})
    sig.create("u@x.ru", "широкий", {"min_value_rub": 1_000_000}, kind="block",
               tg_target_id=1)
    sig.create("u@x.ru", "узкий", {"min_value_rub": 50_000_000}, kind="block",
               tg_target_id=1)
    _seed(pdb, [(1, "RU000FLOAT01", 60_000_000)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 1
    assert [p["filter_name"] for p in sent] == ["широкий"]
