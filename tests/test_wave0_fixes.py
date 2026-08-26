"""Регресс-щит волны 0 аудита 2026-08-26 (docs/fix_plan_2026-08-26.md).

Каждый тест падает на коде ДО фикса — это и есть их смысл."""
import sqlite3
from datetime import date

import pytest


# ─── alor_ws: _tick_value отдаёт кортеж, в агрегат идёт число ────────────────

def test_tick_value_is_tuple_and_aggregate_takes_number():
    """alor_ws передавал КОРТЕЖ в value= → TypeError на КАЖДОЙ сделке.

    Исключение уносило весь сеанс (внешний except накрывает while), вместе с
    подписками на стаканы, а backoff сбрасывался в 1 → шторм реконнектов."""
    from services.trades_stream import _tick_value
    from services import live_quotes as lq

    v = _tick_value("RU000A100001", 100.0, 10)
    assert isinstance(v, tuple) and len(v) == 2      # сигнатура зафиксирована
    val, fx_ok = v
    assert isinstance(val, float) and isinstance(fx_ok, bool)

    lq.drop("RU000A100001")
    lq.add_trade("RU000A100001", 100.0, 10, tid="t1", value=val)
    got = lq.get("RU000A100001")
    assert got is not None
    assert got["trades"] == 1                        # до фикса падало ДО n += 1
    assert got["val_today"] == round(val)
    lq.drop("RU000A100001")


def test_tuple_value_still_raises_in_aggregate():
    """ПОВЕДЕНЧЕСКИЙ контроль исходного бага: кортеж в value= обязан бросать.

    Именно это и происходило в alor_ws на КАЖДОЙ сделке — тест фиксирует, что
    поведение накопителя не изменилось, а чинить надо было вызывающего."""
    from services import live_quotes as lq
    lq.drop("RU000A100002")
    with pytest.raises(TypeError):
        lq.add_trade("RU000A100002", 100.0, 10, tid="a", value=(5000.0, True))
    lq.drop("RU000A100002")


def test_alor_ws_message_handling_is_isolated():
    """Битое сообщение не должно рвать сеанс: обработка одного сообщения обёрнута
    в собственный try, а разрыв сокета и CancelledError — снаружи."""
    import ast
    import inspect
    from services import alor_ws
    src = inspect.getsource(alor_ws.alor_orderbook_ws)
    tree = ast.parse(src.replace("\n" + " " * 0, "\n", 1).lstrip())
    # найти try, внутри которого есть ветка chan == "ob"
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "broadcast_orderbook" in body and "broadcast_market_data" in body:
            # ловим Exception (не BaseException — иначе съест CancelledError)
            names = [h.type.id for h in node.handlers
                     if isinstance(h.type, ast.Name)]
            assert names == ["Exception"], names
            found = True
    assert found, "обработка сообщения не изолирована собственным try"


# ─── cbr: пустая история КС не пиннится на сутки ────────────────────────────

def test_empty_ks_not_pinned_for_the_day(monkeypatch):
    """`[]` проходил проверку `is not None` и пиннился до полуночи:
    ks_rate_at() отдавал None для ВСЕХ фиксингов KEYRATE весь день."""
    from services import cbr
    today = date.today().isoformat()
    monkeypatch.setitem(cbr._mem, "date", today)
    monkeypatch.setitem(cbr._mem, "ks", [])
    monkeypatch.setitem(cbr._mem, "ts", 0.0)         # ретрай давно протух
    assert cbr._mem_fresh(today) is False            # → будет новая попытка


def test_empty_ks_retry_is_throttled(monkeypatch):
    """...но не на каждый вызов: _refresh() зовётся из _index_provider на каждую
    бумагу и из ks_rate_at на каждый купон — это тысячи requests под локом."""
    import time
    from services import cbr
    today = date.today().isoformat()
    monkeypatch.setitem(cbr._mem, "date", today)
    monkeypatch.setitem(cbr._mem, "ks", [])
    monkeypatch.setitem(cbr._mem, "ts", time.time())  # только что пробовали
    assert cbr._mem_fresh(today) is True              # ретрай отложен


def test_non_empty_ks_is_fresh(monkeypatch):
    from services import cbr
    today = date.today().isoformat()
    monkeypatch.setitem(cbr._mem, "date", today)
    monkeypatch.setitem(cbr._mem, "ks", [(date(2026, 1, 1), 16.0)])
    assert cbr._mem_fresh(today) is True


# ─── схема реестра: пропущенные запятые ─────────────────────────────────────

def test_schema_has_no_missing_commas():
    """Пропущенная запятая в CREATE TABLE SQLite не ломает, а СЪЕДАЕТ: две
    колонки склеиваются в одну с многословным типом (так пропала cap_pct —
    её возвращал только ALTER TABLE из _MIGRATIONS)."""
    from services import instruments_registry as reg
    c = sqlite3.connect(":memory:")
    c.executescript(reg._SCHEMA)
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for tbl in tables:
        for r in c.execute(f"PRAGMA table_info({tbl})"):
            typ = (r[2] or "")
            assert len(typ.split()) <= 2, f"{tbl}.{r[1]}: съеденная запятая, тип {typ!r}"
    cols = [r[1] for r in c.execute("PRAGMA table_info(instruments)")]
    assert "cap_pct" in cols and "floor_pct" in cols


# ─── signals: limit не снимает лимит ────────────────────────────────────────

def test_events_limit_is_clamped(tmp_path, monkeypatch):
    """Отрицательный limit в SQLite = LIMIT -1 = «без ограничения».

    Проверяем ПОВЕДЕНИЕ: с limit=-1 функция не должна отдать больше потолка."""
    from services import signals
    got = signals.events_for_user("никого@нет.ru", limit=-1)
    assert isinstance(got, list) and len(got) <= 500
    got0 = signals.events_for_user("никого@нет.ru", limit=0)
    assert isinstance(got0, list) and len(got0) <= 500


def test_events_route_declares_bounds():
    from api.routes import signals as route
    import inspect
    sig = inspect.signature(route.list_events)
    default = sig.parameters["limit"].default
    # в этой версии FastAPI границы лежат в annotated-metadata, а не атрибутами
    bounds = {type(m).__name__: getattr(m, type(m).__name__.lower(), None)
              for m in getattr(default, "metadata", [])}
    assert bounds.get("Ge") == 1, bounds
    assert bounds.get("Le") == 500, bounds


# ─── backdate: сбой MOEX больше не стирает историю ──────────────────────────

def _seed_stale(pdb, isin, dates, engine_ver):
    """Кладёт honest-строки СТАРОЙ версии движка."""
    from services.spread_history import upsert_honest
    pts = [{"date": d, "price_pct": 100.0, "y_idx_bps": 200,
            "dm_bps": 180, "yield_pct": 16.0} for d in dates]
    upsert_honest(isin, pts, set(), engine_ver)


def test_stale_rows_survive_calculation_failure(tmp_path, monkeypatch):
    """ГЛАВНОЕ свойство фикса: сбой MOEX не стирает историю.

    Раньше drop_stale_honest сносил стейл АВАНСОМ, в самом начале функции.
    honest_spread_series следом кидала «история MOEX за окно пуста», вызывающий
    (api/routes/history.py) это только логировал — и первый же заход на график
    стирал год истории, не записав ничего взамен."""
    import asyncio
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "portfolio.db")
    pdb.init_db()

    from services import backdate
    from services.exceptions import CalculationException
    from services.spread_history import read_history

    isin = "RU_TEST_STALE_1"
    dates = ["2026-08-10", "2026-08-11", "2026-08-12"]
    _seed_stale(pdb, isin, dates, backdate.HONEST_ENGINE_VERSION - 1)
    assert len(read_history(isin, days=400)) == 3

    async def _boom(*a, **kw):
        raise CalculationException("история MOEX за окно пуста")

    monkeypatch.setattr(backdate, "honest_spread_series", _boom)
    monkeypatch.setattr(backdate, "_backfill_done", {})
    with pytest.raises(CalculationException):
        asyncio.run(backdate.ensure_honest_backfill(isin, days=30))

    # ИСТОРИЯ НА МЕСТЕ — до фикса тут было 0 строк
    assert len(read_history(isin, days=400)) == 3


def test_stale_rows_replaced_on_healthy_run(tmp_path, monkeypatch):
    """Здоровый прогон обязан заменить стейл-строки, а не оставить их."""
    import asyncio
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "portfolio.db")
    pdb.init_db()

    from services import backdate
    from services.spread_history import read_history

    isin = "RU_TEST_STALE_2"
    dates = ["2026-08-10", "2026-08-11"]
    _seed_stale(pdb, isin, dates, backdate.HONEST_ENGINE_VERSION - 1)

    async def _ok(isin_, span, board, **kw):
        pts = [{"date": d, "price_pct": 101.0, "y_idx_bps": 300,
                "dm_bps": 280, "yield_pct": 17.0} for d in dates]
        on_chunk = kw.get("on_chunk")
        if on_chunk:
            on_chunk(pts)
        return {"points": pts}

    monkeypatch.setattr(backdate, "honest_spread_series", _ok)
    monkeypatch.setattr(backdate, "_backfill_done", {})
    asyncio.run(backdate.ensure_honest_backfill(isin, days=30))

    rows = {r["date"]: r for r in read_history(isin, days=400)}
    assert len(rows) == 2
    for d in dates:
        # строка перештампована новой версией и новым числом
        assert rows[d]["engine_ver"] == backdate.HONEST_ENGINE_VERSION
        assert rows[d]["y_idx"] == 300


# ─── cbr: куцый фетч не убивает историю ни в файле, ни в памяти ─────────────

def test_short_fetch_merges_cache_into_memory(tmp_path, monkeypatch):
    """Куцый фетч RUONIA обязан подмешать кэш И В ФАЙЛ, И В МЁРЖ.

    _save_cache пишет файл целиком: фолбэк _fetch_ruonia_current_live (~2 точки)
    сносил историю насовсем. Но защитить только файл мало — если в мёрж уйдёт
    куцый список, дыра seed_end↔live останется в ПАМЯТИ на весь день и отравит
    окна фиксинга всех RUONIA-флоатеров стейл-ставкой."""
    from datetime import date as _date
    from services import cbr

    cache_file = tmp_path / "cbr_cache.json"
    monkeypatch.setattr(cbr, "_CACHE", str(cache_file))
    # в кэше — длинная история
    old = [[f"2026-0{m}-01", 13.0 + m] for m in range(1, 8)]
    cache_file.write_text(__import__("json").dumps(
        {"cache_date": "2026-08-25", "ks": [], "ruonia_live": old}), encoding="utf-8")

    monkeypatch.setattr(cbr, "_fetch_ks_live", lambda: [(_date(2026, 8, 26), 14.0)])
    # dynamics падает → фолбэк отдаёт 2 точки
    def _boom(*a, **kw):
        raise RuntimeError("dynamics down")
    monkeypatch.setattr(cbr, "_fetch_ruonia_dynamics", _boom)
    monkeypatch.setattr(cbr, "_fetch_ruonia_current_live",
                        lambda: [(_date(2026, 8, 26), 13.85)])
    monkeypatch.setattr(cbr, "_load_seed", lambda: [])
    monkeypatch.setattr(cbr, "_load_rc_ruonia", lambda: [])
    monkeypatch.setattr(cbr, "_mem", {"date": None, "ks": None, "ruonia": None, "ts": 0.0})

    cbr._refresh_locked(_date.today().isoformat())

    saved = __import__("json").loads(cache_file.read_text(encoding="utf-8"))
    # ФАЙЛ: история цела (7 старых + 1 новая), а не 1 точка
    assert len(saved["ruonia_live"]) == 8, saved["ruonia_live"]
    # ПАМЯТЬ: тот же набор — иначе дыра остаётся в расчёте
    assert len(cbr._mem["ruonia"]) == 8, cbr._mem["ruonia"]
