"""Страница ленты: топ-N по веткам и итоги одним проходом обязаны давать то же,
что простое объединение двух таблиц (14.10.2026: ускорение /api/trades с 6 с).

Эталон — наивный Python: блоки + тики, которых нет в блоках, сортировка по
(ts, trade_id) вниз. Данные случайные, но с теми же ловушками, что в проде:
одинаковый trade_id в обеих таблицах (дубль принта), одинаковый ts у десятков
сделок, адресные сделки, валютные расчёты, большой набор бумаг (временная
таблица) и маленький (плейсхолдеры)."""
import importlib
import random
from datetime import date, timedelta

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.block_trades as bt
    import services.tape as tape
    importlib.reload(bt)
    importlib.reload(tape)
    yield pdb, tape
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)


def _fill(pdb, seed=7, n_isins=700, n=3000):
    rnd = random.Random(seed)
    isins = [f"RU000A{i:06d}" for i in range(n_isins)]
    today = date.today()
    blocks, ticks = {}, {}
    with pdb._connect() as c:
        for tid in range(1, n + 1):
            day = today - timedelta(days=rnd.randrange(0, 12))
            # узкий набор секунд — много сделок с одинаковым ts
            ts = f"{day.isoformat()} 1{rnd.randrange(0, 4)}:00:{rnd.randrange(0, 5):02d}"
            isin = rnd.choice(isins[:60]) if rnd.random() < .6 else rnd.choice(isins)
            val = rnd.choice([1e3, 5e4, 3e5, 2e6, 12e6, 60e6])
            side = rnd.choice(["buy", "sell"])
            kind = rnd.random()
            if kind < .25:                      # только блок (адресная — иногда)
                mkt = "ndm" if rnd.random() < .3 else "bonds"
                cur = rnd.choice([None, "SUR", "SUR", "CNY"])
                c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                          "qty,value,side,cur,y_idx_bps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (tid, isin, isin, ts, mkt, "TQCB", 100.0, 1.0, val, side, cur,
                           rnd.choice([None, 150.0, 400.0])))
                blocks[tid] = (ts, isin, val, side, mkt, cur)
            elif kind < .5:                     # принт и в блоках, и в тиках (дубль)
                c.execute("INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,price,"
                          "qty,value,side,cur) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (tid, isin, isin, ts, "bonds", "TQCB", 100.0, 1.0, val, side, "SUR"))
                c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                          "VALUES(?,?,?,?,?,?,?,?)", (isin, tid, ts, 100.0, 1.0, val, side, "TQCB"))
                blocks[tid] = (ts, isin, val, side, "bonds", "SUR")
            else:                               # только тик
                c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
                          "VALUES(?,?,?,?,?,?,?,?)", (isin, tid, ts, 100.0, 1.0, val, side, "TQCB"))
                ticks[tid] = (ts, isin, val, side, "bonds", "SUR")
    return isins, blocks, ticks


def _ref(blocks, ticks, frm, min_value=0, isins=None, side=None, market=None):
    rows = dict(blocks)
    for tid, v in ticks.items():
        rows.setdefault(tid, v)               # тик, которого нет в блоках
    out = []
    for tid, (ts, isin, val, sd, mkt, cur) in rows.items():
        if ts < frm or (min_value and val < min_value * 0.9):
            continue
        if isins is not None and isin not in isins:
            continue
        if side and sd != side:
            continue
        if market and mkt != market:
            continue
        out.append((ts, tid, isin, val, cur))
    out.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return out


def _frm(days):
    return (date.today() - timedelta(days=days)).isoformat()


@pytest.mark.parametrize("min_value", [0, 3e5, 6e6])          # ниже и выше порога топ-N
@pytest.mark.parametrize("subset", ["none", "small", "big"])  # без набора / плейсхолдеры / tmp
def test_page_matches_reference(env, min_value, subset):
    pdb, tape = env
    isins, blocks, ticks = _fill(pdb)
    sel = {"none": None, "small": isins[:20], "big": isins[:600]}[subset]   # >400 → tmp
    got = tape.read_tape(frm=_frm(10), min_value=min_value, isins=sel, limit=200)
    ref = _ref(blocks, ticks, _frm(10), min_value, set(sel) if sel else None)[:200]
    assert [(r["ts"], r["trade_id"]) for r in got] == [(r[0], r[1]) for r in ref]


def test_cursor_pages_stitch_without_gaps_or_dupes(env):
    pdb, tape = env
    isins, blocks, ticks = _fill(pdb)
    seen, cur = [], {}
    for _ in range(40):
        page = tape.read_tape(frm=_frm(10), limit=150, isins=isins[:600], **cur)
        if not page:
            break
        seen += [(r["ts"], r["trade_id"]) for r in page]
        cur = {"before_ts": page[-1]["ts"], "before_id": page[-1]["trade_id"]}
    ref = [(r[0], r[1]) for r in _ref(blocks, ticks, _frm(10), 0, set(isins[:600]))]
    assert seen == ref


def test_side_and_market_filters(env):
    pdb, tape = env
    isins, blocks, ticks = _fill(pdb)
    got = tape.read_tape(frm=_frm(10), side="sell", limit=300, isins=isins[:600])
    ref = _ref(blocks, ticks, _frm(10), 0, set(isins[:600]), side="sell")[:300]
    assert [(r["ts"], r["trade_id"]) for r in got] == [(r[0], r[1]) for r in ref]
    ndm = tape.read_tape(frm=_frm(10), market="ndm", limit=300)
    assert {r["market"] for r in ndm} == {"ndm"}


def test_stats_one_pass_totals_and_top(env):
    pdb, tape = env
    isins, blocks, ticks = _fill(pdb)
    st = tape.tape_stats(frm=_frm(10), isins=isins[:600])
    ref = _ref(blocks, ticks, _frm(10), 0, set(isins[:600]))
    assert st["n"] == len(ref)
    rub = [r for r in ref if r[4] in (None, "SUR")]
    assert st["value"] == pytest.approx(sum(r[3] for r in rub))
    per = {}
    for r in rub:
        per[r[2]] = per.get(r[2], 0) + r[3]
    want = sorted(per.items(), key=lambda kv: -kv[1])[:3]
    assert [t["value"] for t in st["top"][:3]] == pytest.approx([v for _, v in want])


def test_big_subset_really_uses_temp_table(env):
    """Если порог _INLINE_ISINS вырастет, «большой» набор перестанет проверять
    путь через временную таблицу — тест должен сказать об этом сам."""
    from services.block_trades import _INLINE_ISINS
    assert 600 > _INLINE_ISINS
