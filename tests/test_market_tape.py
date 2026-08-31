"""Общерыночная лента сделок (вкладка СДЕЛКИ).

Ключевое отличие от ленты выпуска: фильтр идёт по времени БЕЗ isin, а итоги
окна считаются по ВСЕМ подходящим сделкам, а не по срезанным лимитом строкам —
иначе «оборот» врал бы ровно на хвост, который не влез на страницу.
"""
import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def ta(tmp_path, monkeypatch):
    """services.trades_archive на пустой временной БД."""
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.trades_archive as mod
    importlib.reload(mod)
    yield mod
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)
    importlib.reload(mod)


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _add(ta, isin, tid, days_ago, value, hour=12, side="buy"):
    with ta._lock, ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (isin, tid, f"{_iso(days_ago)} {hour:02d}:00:00", 100.0, 1.0,
                   value, side, "TQCB"))


def test_tape_across_isins_newest_first(ta):
    _add(ta, "RU000A0000A1", 1, 0, 1_000, hour=10)
    _add(ta, "RU000A0000B2", 2, 0, 2_000, hour=15)
    _add(ta, "RU000A0000C3", 3, 5, 3_000)

    rows = ta.read_tape(frm=_iso(0))
    assert [r["isin"] for r in rows] == ["RU000A0000B2", "RU000A0000A1"]  # новые сверху


def test_tape_filters(ta):
    _add(ta, "RU000A0000A1", 1, 0, 500_000, side="buy")
    _add(ta, "RU000A0000B2", 2, 0, 5_000_000, side="sell")

    assert len(ta.read_tape(frm=_iso(0), min_value=1_000_000)) == 1
    assert len(ta.read_tape(frm=_iso(0), side="buy")) == 1
    assert len(ta.read_tape(frm=_iso(0), isins=["RU000A0000B2"])) == 1
    assert len(ta.read_tape(frm=_iso(0), isins=["RU000A0000A1", "RU000A0000B2"])) == 2


def test_stats_count_whole_window_not_page(ta):
    """Лимит режет строки, но не итоги: иначе оборот занижался бы на хвост."""
    for i in range(10):
        _add(ta, "RU000A0000A1", i, 0, 1_000, hour=10 + i % 8)

    rows = ta.read_tape(frm=_iso(0), limit=3)
    stats = ta.tape_stats(frm=_iso(0))
    assert len(rows) == 3
    assert stats["n"] == 10
    assert stats["value"] == pytest.approx(10_000)


def test_stats_sides_and_top(ta):
    _add(ta, "RU000A0000A1", 1, 0, 7_000, side="buy")
    _add(ta, "RU000A0000A1", 2, 0, 1_000, side="sell")
    _add(ta, "RU000A0000B2", 3, 0, 2_000, side="sell")

    s = ta.tape_stats(frm=_iso(0), top=2)
    assert s["buy_value"] == pytest.approx(7_000)
    assert s["sell_value"] == pytest.approx(3_000)
    assert [t["isin"] for t in s["issuers_top"]] == ["RU000A0000A1", "RU000A0000B2"]


def test_stats_respect_same_filter_as_rows(ta):
    """Итоги и строки обязаны считаться по ОДНОМУ фильтру."""
    _add(ta, "RU000A0000A1", 1, 0, 500_000)
    _add(ta, "RU000A0000B2", 2, 0, 5_000_000)

    rows = ta.read_tape(frm=_iso(0), min_value=1_000_000)
    s = ta.tape_stats(frm=_iso(0), min_value=1_000_000)
    assert len(rows) == s["n"] == 1
    assert s["value"] == pytest.approx(5_000_000)


# --- лестница окон и подсказка индекса (производительность, см. services/tape) ---

def test_window_escalation_matches_full_scan(ta):
    """Страница, собранная эскалацией окон, совпадает с прямым запросом на всё
    окно. Иначе оптимизация тихо теряла бы старые сделки, когда свежих мало."""
    from services import tape as tape_svc
    # сделки редкие и старые: узкие окна (7/30/120 дней) пустые, страницу
    # набирает только полное окно
    for i, days in enumerate([200, 250, 300, 330]):
        _add(ta, "RU000A0000A1", 900 + i, days, 5e6)

    frm = _iso(400)
    with_steps = tape_svc.read_tape(frm=frm, limit=10)
    saved = tape_svc._WINDOW_STEPS
    tape_svc._WINDOW_STEPS = ()
    try:
        direct = tape_svc.read_tape(frm=frm, limit=10)
    finally:
        tape_svc._WINDOW_STEPS = saved

    assert [r["trade_id"] for r in with_steps] == [r["trade_id"] for r in direct]
    assert len(with_steps) == 4


def test_window_escalation_stops_on_full_page(ta):
    """Свежих сделок хватает на страницу — старое окно не открывается вовсе
    (порядок по времени вниз, старые в страницу всё равно не попадут)."""
    from services import tape as tape_svc
    for i in range(5):
        _add(ta, "RU000A0000A1", 800 + i, 1, 3e6, hour=10 + i)
    _add(ta, "RU000A0000A1", 700, 300, 9e6)      # старая, в страницу не должна влезть

    rows = tape_svc.read_tape(frm=_iso(400), limit=5)
    assert len(rows) == 5
    assert 700 not in [r["trade_id"] for r in rows]


def test_value_index_hint_used_only_for_wide_sets(ta):
    """Подсказка INDEXED BY ставится для широкой выборки с порогом и НЕ ставится
    для одной бумаги: там правильный план как раз по (isin, ts)."""
    from services import tape as tape_svc
    wide, _ = tape_svc._union("2026-01-01", None, 1e7, None, None, None, None, False)
    assert "INDEXED BY ix_block_value_ts" in wide

    narrow, _ = tape_svc._union("2026-01-01", None, 1e7, None, None, ["RU000A0000A1"],
                                None, False)
    assert "INDEXED BY" not in narrow

    no_thr, _ = tape_svc._union("2026-01-01", None, 0, None, None, None, None, False)
    assert "INDEXED BY" not in no_thr


def test_isin_tape_still_works_with_threshold(ta):
    """Лента одной бумаги с порогом — та же, что без подсказки индекса."""
    from services import tape as tape_svc
    _add(ta, "RU000A0000A1", 601, 2, 2e7)
    _add(ta, "RU000A0000A1", 602, 2, 5e5)
    rows = tape_svc.read_isin_trades("RU000A0000A1", frm=_iso(10), min_value=1e6)
    assert [r["trade_id"] for r in rows] == [601]


def _add_block(ta, isin, tid, days_ago, value, market="ndm", board="PSOB",
               price=100.0, side=None):
    """Строка ISS-ленты: адресные (РПС) живут только в ней — у брокера подписки
    на них нет в принципе."""
    with ta._lock, ta._connect() as c:
        c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                  "qty,value,side,cur) VALUES(?,?,?,?,?,?,?,?,?,?,'SUR')",
                  (tid, isin, isin, f"{_iso(days_ago)} 12:00:00", market, board,
                   price, 1.0, value, side))


def test_isin_tape_hides_negotiated_by_default(ta):
    """Дефолт — только безадресные: маркерам на графике адресные рисует свой
    слой, и без фильтра одна сделка получила бы два маркера."""
    from services import tape as tape_svc
    _add(ta, "RU000A0000A1", 901, 1, 2e6)
    _add_block(ta, "RU000A0000A1", 902, 1, 9e6)
    rows = tape_svc.read_isin_trades("RU000A0000A1", frm=_iso(10))
    assert [r["trade_id"] for r in rows] == [901]


def test_isin_tape_market_none_adds_negotiated(ta):
    """market=None — лента карточки: РПС видно рядом со стаканом, помечено."""
    from services import tape as tape_svc
    _add(ta, "RU000A0000A1", 903, 1, 2e6)
    _add_block(ta, "RU000A0000A1", 904, 1, 9e6)
    rows = tape_svc.read_isin_trades("RU000A0000A1", frm=_iso(10), market=None)
    assert [r["trade_id"] for r in rows] == [903, 904]
    assert [r["negotiated"] for r in rows] == [False, True]
    assert tape_svc.count_isin_trades("RU000A0000A1", _iso(10), None, 0, None,
                                      None) == 2
