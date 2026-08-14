"""Колокольчик по крупным сделкам: звонят только флоатеры.

Крупняк рынка в рублях — это почти целиком ОФЗ-ПД, поэтому без фильтра
уведомления вырождались в ленту фиксов. Отдельно проверяется, что водяной знак
двигается и по отфильтрованным сделкам: иначе пачка фиксов подряд встала бы
перед выборкой намертво и флоатер за ней не позвонил бы никогда.
"""
import asyncio
import importlib
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


def _seed(pdb, rows):
    d = date.today().isoformat()
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,qty,"
            "value,yld,side,face,cur) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(tid, isin, isin, f"{d} 10:0{i}:00", "bonds", "TQCB", 100.0, 1000,
              val, None, "buy", 1000, "SUR")
             for i, (tid, isin, val) in enumerate(rows)])


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
    # знак ушёл за обе сделки — фикс больше не попадёт в выборку
    assert mod.get_cursor("alert") == 2
    assert mod.pending_alerts() == []


def test_only_fixed_still_moves_watermark(bt, monkeypatch):
    """Пачка одних фиксов: звонков нет, но знак сдвинут — иначе залипание."""
    pdb, mod = bt
    big = mod.BLOCK_ALERT_MIN_RUB
    _seed(pdb, [(1, "RU000FIXED01", big + 1), (2, "RU000FIXED01", big + 2)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0
    assert sent == []
    assert mod.get_cursor("alert") == 2


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
    assert mod.get_cursor("alert") == 2


def test_disabled_filter_does_not_fall_back_to_default(bt, sig, monkeypatch):
    """Выключенный фильтр — не «нет фильтров»: умолчание не воскресает."""
    pdb, mod = bt
    f = sig.create("u@x.ru", "тихий", {"min_value_rub": 5_000_000}, kind="block")
    sig.update("u@x.ru", f["id"], enabled=False)
    _seed(pdb, [(1, "RU000FLOAT01", mod.BLOCK_ALERT_MIN_RUB + 1)])
    sent: list = []
    assert _run(mod, monkeypatch, sent) == 0
    assert mod.get_cursor("alert") == 1


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
    assert mod.get_cursor("alert") == 1        # знак всё равно сдвинут
