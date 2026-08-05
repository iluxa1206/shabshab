"""Инкрементальный дрейн тиков: окно от водяного знака, а не «всегда 30 дней».

Раньше каждый вызов (открытие графика, часовой демон по всему юниверсу) качал
полное окно брокера заново — на ликвидной бумаге это сотни тысяч сделок и 15
страниц пагинации на запрос. Тесты держат два инварианта, которые молча ломают
архив: знак двигается ТОЛЬКО на полностью вычитанном окне и ТОЛЬКО вперёд.
"""
import asyncio
import importlib
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def ta(tmp_path, monkeypatch):
    """services.trades_archive на пустой временной БД, без сети."""
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


@pytest.fixture
def calls(ta, monkeypatch):
    """Заглушки сети. calls['frm'] — левая граница, с которой дрейн пошёл в Alor."""
    import services.bars as bars
    import services.backdate as backdate
    rec: dict = {"frm": None, "to": None, "today_hits": 0}

    async def _fake_history(client, isin, headers, frm, to):
        rec["frm"], rec["to"] = frm, to
        return list(rec.get("rows") or []), rec.get("complete", True)

    async def _fake_today(client, isin, headers):
        rec["today_hits"] += 1
        return []

    async def _fake_headers(force=False):
        return {"Authorization": "Bearer test"}

    async def _fake_resolve(isin, board=None):
        return isin, "TQCB"

    async def _fake_faces(client, secid, board, frm, till):
        return {}

    monkeypatch.setattr(ta, "fetch_history", _fake_history)
    monkeypatch.setattr(ta, "fetch_today", _fake_today)
    monkeypatch.setattr(ta, "_headers", _fake_headers)
    monkeypatch.setattr(backdate, "resolve_market", _fake_resolve)
    monkeypatch.setattr(bars, "fetch_daily_face", _fake_faces)
    return rec


ISIN = "RU000A100001"


def _msk_now(ta):
    return datetime.now(ta._MSK)


def _yesterday_end(ta) -> str:
    d = _msk_now(ta).date()
    return (datetime.combine(d, datetime.min.time(), ta._MSK)
            - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


def test_cold_start_takes_full_broker_window(ta, calls):
    asyncio.run(ta.drain(ISIN))
    expect = _msk_now(ta).date() - timedelta(days=ta.ALOR_HISTORY_DAYS)
    assert calls["frm"].date() == expect
    # окно вычитано целиком → знак встал на конец вчера
    assert ta.get_watermark(ISIN) == _yesterday_end(ta)


def test_second_run_starts_from_watermark(ta, calls):
    asyncio.run(ta.drain(ISIN))
    first_frm = calls["frm"]
    asyncio.run(ta.drain(ISIN))
    second_frm = calls["frm"]

    assert second_frm > first_frm
    mark = datetime.fromisoformat(ta.get_watermark(ISIN)).replace(tzinfo=ta._MSK)
    assert second_frm == mark - timedelta(hours=ta.TICK_DRAIN_OVERLAP_HOURS)
    # сегодняшняя сессия тянется всё равно каждый раз — /history её не отдаёт
    assert calls["today_hits"] == 2


def test_incomplete_fetch_does_not_move_watermark(ta, calls):
    calls["complete"] = False
    asyncio.run(ta.drain(ISIN))
    assert ta.get_watermark(ISIN) is None

    # следующий заход снова берёт полное окно, хвост не потерян
    asyncio.run(ta.drain(ISIN))
    assert calls["frm"].date() == _msk_now(ta).date() - timedelta(days=ta.ALOR_HISTORY_DAYS)


def test_silent_window_still_advances_watermark(ta, calls):
    """Неликвид без сделок: «тишина» тоже вычитана. Иначе такая бумага качала бы
    пустой месяц при каждом прогоне демона вечно."""
    calls["rows"] = []
    asyncio.run(ta.drain(ISIN))
    assert ta.get_watermark(ISIN) == _yesterday_end(ta)


def test_full_ignores_watermark(ta, calls):
    asyncio.run(ta.drain(ISIN))
    asyncio.run(ta.drain(ISIN, full=True))
    assert calls["frm"].date() == _msk_now(ta).date() - timedelta(days=ta.ALOR_HISTORY_DAYS)


def test_watermark_never_goes_backwards(ta):
    ta.set_watermark(ISIN, "2026-08-01 23:59:59")
    ta.set_watermark(ISIN, "2026-07-01 23:59:59")
    assert ta.get_watermark(ISIN) == "2026-08-01 23:59:59"


def test_days_caps_cold_window(ta, calls):
    asyncio.run(ta.drain(ISIN, days=5))
    assert calls["frm"].date() == _msk_now(ta).date() - timedelta(days=5)


def test_seed_watermarks_from_existing_archive(ta):
    """Архив, накопленный до инкрементального режима, получает знак из своих же
    тиков — иначе каждая бумага один раз сходила бы за полным месяцем."""
    older, newer = "2026-07-20 10:00:00", "2026-07-28 15:30:00"
    with ta._connect() as c:
        for i, (isin, ts) in enumerate([(ISIN, older), (ISIN, newer),
                                        ("RU000A100002", older)]):
            c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value) "
                      "VALUES(?,?,?,?,?,?)", (isin, i + 1, ts, 100.0, 1.0, 1000.0))

    assert ta.seed_watermarks() == 2
    assert ta.get_watermark(ISIN) == newer                  # максимум, не минимум
    assert ta.get_watermark("RU000A100002") == older
    assert ta.seed_watermarks() == 0                        # идемпотентно


def test_seed_does_not_override_live_watermark(ta):
    ta.set_watermark(ISIN, "2026-08-01 23:59:59")
    with ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value) "
                  "VALUES(?,?,?,?,?,?)", (ISIN, 1, "2026-07-01 10:00:00", 100.0, 1.0, 1.0))
    ta.seed_watermarks()
    assert ta.get_watermark(ISIN) == "2026-08-01 23:59:59"


def test_prune_does_not_touch_watermark(ta):
    """Ради этого знак и живёт в отдельной таблице: ретеншен чистит старые тики,
    но точка старта дрейна обязана остаться на месте."""
    ta.set_watermark(ISIN, "2026-08-01 23:59:59")
    old = (date.today() - timedelta(days=100)).isoformat()
    with ta._connect() as c:
        c.execute("INSERT INTO trade_tick(isin,trade_id,ts,price,qty,value) "
                  "VALUES(?,?,?,?,?,?)", (ISIN, 1, f"{old} 10:00:00", 100.0, 1.0, 5000.0))

    assert ta.prune(raw_days=35, big_value=1_000_000)["deleted"] == 1
    assert ta.get_watermark(ISIN) == "2026-08-01 23:59:59"
