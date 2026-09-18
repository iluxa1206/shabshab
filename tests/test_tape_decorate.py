"""Разметка строк ленты: базы недели и вид режима торгов."""
from datetime import date, timedelta

from api.routes.trades import _decorate
from services.block_trades import board_kind, tag_board


def _row(ts: str, board: str = "TQCB", market: str = "bonds") -> dict:
    return {"isin": "X", "ts": ts, "board": board, "market": market, "price": 100.0}


def test_bases_only_within_window():
    """База привязана к сегодняшней дате: строка старше окна с ней не
    сравнивается — иначе августовский принт «отклонялся» от сентябрьской цены."""
    today = date.today()
    cutoff = (today - timedelta(days=7)).isoformat()
    fresh = _row(f"{today.isoformat()} 10:00:00")
    edge = _row(f"{cutoff} 10:00:00")
    stale = _row(f"{(today - timedelta(days=8)).isoformat()} 10:00:00")
    bases = {"X": {"spread": 150.0, "price": 99.5}}
    _decorate([fresh, edge, stale], {}, {}, {}, bases, cutoff)
    assert (fresh["y_idx_avg7_bps"], fresh["price_avg7_pct"]) == (150.0, 99.5)
    assert (edge["y_idx_avg7_bps"], edge["price_avg7_pct"]) == (150.0, 99.5)
    assert (stale["y_idx_avg7_bps"], stale["price_avg7_pct"]) == (None, None)


def test_board_kind_is_machine_field():
    """Размещение/выкуп — адресные, но не РПС: вид отдаётся машинным полем,
    подпись — отдельно, и витрина не разбирает русский текст."""
    assert board_kind("PSAU") == "placement"
    assert board_kind("PAUS", "ndm") == "placement"
    assert board_kind("AUCT") == "placement"
    assert board_kind("PSBB") == "buyback"
    assert board_kind("PSOB") == "rps"
    assert board_kind("PTOB") == "rps_cc"
    assert board_kind("TQCB") == "book"
    assert board_kind("XXXX", "ndm") == "rps"        # незнакомый борд ndm — адресный
    assert board_kind("XXXX") == "other"
    r = _row("2026-09-18 10:00:00", "PSAU", "ndm")
    tag_board(r)
    assert (r["board_short"], r["board_title"], r["board_kind"]) == ("Размещ.", "Размещение", "placement")
