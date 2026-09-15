"""Ручки витрины ОФЗ (docs/ofz_desk_tz.md): КБД на дату с шагом назад через
выходные, классификация бордов в объёмах дня и приоритет источников as-of
(снапшот > пересчёт по цене дня). Всё без сети: ISS подменён, база — tmp.
"""
import asyncio
from datetime import date, timedelta

import pytest

from services import market_data as md
from services import block_trades as bt


FRI, SAT, SUN = "2026-09-11", "2026-09-12", "2026-09-13"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import services.portfolio_db as pdb
    monkeypatch.setattr(pdb, "DB_PATH", tmp_path / "t.db")
    pdb.init_db()
    return pdb


class _Resp:
    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows

    def json(self):
        return {"yearyields": {"columns": ["tradedate", "tradetime", "period", "value"],
                               "data": self._rows}}


def _iss_curve(d):
    return [[d, "18:49:55", 1.0, 13.8574], [d, "18:49:55", 2.0, 14.641],
            [d, "18:49:55", 5.0, 15.9177]]


@pytest.fixture()
def fake_iss(monkeypatch):
    """ISS zcyc: пятница с кривой, выходные — пустой data. Считает вызовы."""
    calls = []

    async def _get(client, url, *, params=None, timeout=6.0):
        d = (params or {}).get("date")
        calls.append(d)
        return _Resp(_iss_curve(d) if d == FRI else [])

    monkeypatch.setattr(md, "_moex_get", _get)
    monkeypatch.setattr(md.MarketDataService, "_gcurve_empty_days", set())
    return calls


# ───────────────────────── /api/curves/gcurve?date= ─────────────────────────

def test_gcurve_weekend_steps_back_to_friday(tmp_db, fake_iss):
    from api.routes.curves import get_gcurve
    out = asyncio.run(get_gcurve(date_=SUN))
    assert out["requested"] == SUN and out["curve_date"] == FRI and out["stale"] is True
    assert [p["years"] for p in out["points"]] == [1.0, 2.0, 5.0]
    assert out["points"][0]["yield_pct"] == 13.8574
    # шагали: вс → сб → пт
    assert fake_iss == [SUN, SAT, FRI]
    # пятница легла в архив; повторный запрос пятницы в ISS не ходит
    assert tmp_db.gcurve_read(FRI) == [(1.0, 13.8574), (2.0, 14.641), (5.0, 15.9177)]
    fake_iss.clear()
    out2 = asyncio.run(get_gcurve(date_=FRI))
    assert out2["curve_date"] == FRI and out2["stale"] is False and fake_iss == []


def test_gcurve_empty_days_not_refetched(tmp_db, fake_iss):
    """Прошлый выходной запоминается: второй запрос воскресенья идёт сразу в
    таблицу за пятницей (ISS не трогаем ни за вс, ни за сб)."""
    from api.routes.curves import get_gcurve
    asyncio.run(get_gcurve(date_=SUN))
    fake_iss.clear()
    asyncio.run(get_gcurve(date_=SUN))
    assert fake_iss == []


def test_gcurve_date_validation(tmp_db, fake_iss):
    from fastapi import HTTPException
    from api.routes.curves import get_gcurve
    for bad in ("2026-13-01", "2013-12-31", (date.today() + timedelta(days=1)).isoformat()):
        with pytest.raises(HTTPException) as e:
            asyncio.run(get_gcurve(date_=bad))
        assert e.value.status_code == 400


def test_gcurve_live_persists_today(tmp_db, monkeypatch):
    """Сегодняшняя кривая из get_gcurve тоже ложится в gcurve_daily — под
    tradedate ISS, а не под календарным сегодня."""
    async def _get(client, url, *, params=None, timeout=6.0):
        return _Resp(_iss_curve(FRI))

    monkeypatch.setattr(md, "_moex_get", _get)
    monkeypatch.setattr(md.MarketDataService, "_gcurve", None)
    monkeypatch.setattr(md.MarketDataService, "_gcurve_date", None)
    g = asyncio.run(md.MarketDataService.get_gcurve())
    assert g is not None and g.ok()
    assert tmp_db.gcurve_read(FRI) == [(1.0, 13.8574), (2.0, 14.641), (5.0, 15.9177)]


# ───────────────────────── классификация бордов ─────────────────────────

def test_classify_board():
    assert bt.classify_board("TQOB") == "book"
    assert bt.classify_board("tqoy") == "book"
    assert bt.classify_board("PSOB") == "rps"
    assert bt.classify_board("PTOB") == "rps"
    assert bt.classify_board("PSEU") == "rps"
    assert bt.classify_board("ZZZZ", market="ndm") == "rps"   # ndm сильнее списка
    assert bt.classify_board("AUCT") == "other"
    assert bt.classify_board(None) == "other"
    assert bt.BOARD_LABELS["PSOB"] == "РПС"


OFZ = [{"isin": "RU000A10OFZ1", "secid": "SU26001RMFS1", "cls": "ofz", "board": "TQOB",
        "val_today": 5_000_000.0, "face": 1000.0, "coupon_pct": 10.0, "accrued": 0.0,
        "faceunit": "SUR"},
       {"isin": "RU000A10OFZ2", "secid": "SU26002RMFS2", "cls": "ofz", "board": "TQOB",
        "val_today": 0.0, "face": 1000.0, "coupon_pct": 8.0, "accrued": 0.0,
        "faceunit": "SUR"},
       {"isin": "RU000A10CRP1", "secid": "RU000A10CRP1", "cls": "corp", "board": "TQCB",
        "val_today": 9e9}]


@pytest.fixture()
def uni(monkeypatch):
    monkeypatch.setitem(md.market_cache, "fixed_universe", [dict(u) for u in OFZ])


def _bond_day(c, isin, d, board, value, wap=None, close=None):
    c.execute("INSERT INTO bond_day(isin,date,board,secid,numtrades,value,waprice,close,"
              "volume,face,cur) VALUES(?,?,?,?,1,?,?,?,1,1000,'SUR')",
              (isin, d, board, isin, value, wap, close))


def test_volumes_past_day_by_boards(tmp_db, uni):
    from api.routes.fixed import get_ofz_volumes
    with tmp_db._connect() as c:
        _bond_day(c, "RU000A10OFZ1", FRI, "TQOB", 3_000_000.0)
        _bond_day(c, "RU000A10OFZ1", FRI, "AUCT", 500_000.0)
        c.execute("INSERT INTO block_day(isin,date,board,secid,numtrades,value) "
                  "VALUES(?,?,?,?,1,?)", ("RU000A10OFZ1", FRI, "PSOB", "SU26001RMFS1", 1_000_000.0))
        c.execute("INSERT INTO block_day(isin,date,board,secid,numtrades,value) "
                  "VALUES(?,?,?,?,1,?)", ("RU000A10OFZ1", FRI, "PTOB", "SU26001RMFS1", 250_000.0))
        # корп в той же таблице — в ответ ОФЗ не попадает
        _bond_day(c, "RU000A10CRP1", FRI, "TQCB", 7_000_000.0)
    out = asyncio.run(get_ofz_volumes(date_=FRI))
    assert out["date"] == FRI and out["live"] is False
    assert set(out["items"]) == {"RU000A10OFZ1"}
    it = out["items"]["RU000A10OFZ1"]
    assert it["book"] == 3_000_000.0 and it["rps"] == 1_250_000.0 and it["other"] == 500_000.0
    assert it["total"] == 4_750_000.0
    assert it["boards"] == {"TQOB": 3_000_000.0, "AUCT": 500_000.0,
                            "PSOB": 1_000_000.0, "PTOB": 250_000.0}
    assert out["board_labels"]["PTOB"] == "РПС с ЦК"


def test_volumes_live_uses_val_today_and_ndm_tape(tmp_db, uni):
    """Сегодня: val_today универса = стакан, адресные — суммой из ленты."""
    from api.routes import fixed as fx
    today = fx._msk_today().isoformat()
    with tmp_db._connect() as c:
        c.executemany(
            "INSERT INTO block_trade(trade_id,isin,secid,ts,market,board,value,cur) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(1, "RU000A10OFZ1", "SU26001RMFS1", f"{today} 12:00:00", "ndm", "PSOB", 2e6, "SUR"),
             (2, "RU000A10OFZ1", "SU26001RMFS1", f"{today} 12:05:00", "ndm", "PSOB", 1e6, "SUR"),
             # безадресная сделка ленты — уже внутри val_today, не суммируем
             (3, "RU000A10OFZ1", "SU26001RMFS1", f"{today} 12:06:00", "bonds", "TQOB", 9e6, "SUR"),
             # вчерашняя адресная — не сегодня
             (4, "RU000A10OFZ1", "SU26001RMFS1", f"{FRI} 12:06:00", "ndm", "PSOB", 9e6, "SUR")])
    out = asyncio.run(fx.get_ofz_volumes(date_=None))
    assert out["live"] is True and out["date"] == today
    it = out["items"]["RU000A10OFZ1"]
    assert it["book"] == 5_000_000.0 and it["rps"] == 3_000_000.0 and it["other"] == 0.0
    assert out["items"]["RU000A10OFZ2"]["total"] == 0.0


# ───────────────────────────── /ofz/asof ─────────────────────────────

def _schedule(first_start: date, years: int, coupon: float, face=1000.0):
    """Полугодовой фикс с start/end/value как у bondization MOEX."""
    coupons, s = [], first_start
    for _ in range(years * 2):
        e = date(s.year + (s.month + 6 - 1) // 12, (s.month + 6 - 1) % 12 + 1, s.day)
        coupons.append({"start": s.isoformat(), "end": e.isoformat(),
                        "value": face * coupon / 100 / 2, "valueprc": coupon, "face": face})
        s = e
    return {"coupons": coupons, "amorts": [{"date": s.isoformat(), "value": face}],
            "offers": []}


@pytest.fixture()
def fake_schedules(monkeypatch):
    sched = {"SU26001RMFS1": _schedule(date(2026, 3, 11), 4, 10.0),
             "SU26002RMFS2": _schedule(date(2026, 3, 11), 8, 8.0)}

    async def _full(secid):
        return sched[secid]

    monkeypatch.setattr(md.MarketDataService, "fetch_bond_schedule_full", staticmethod(_full))
    from api.routes import fixed as fx
    fx._asof_memo.clear()
    yield sched
    fx._asof_memo.clear()


def test_asof_snap_beats_reprice_and_steps_back(tmp_db, uni, fake_schedules):
    """ОФЗ1: есть снапшот и цена дня — YTM берётся из снапшота (src=snap), а
    дюрация пересчётом по цене снапшота. ОФЗ2: только цена дня — пересчёт
    движком (src=reprice). Запрос на воскресенье → фактическая пятница."""
    from api.routes.fixed import get_ofz_asof
    with tmp_db._connect() as c:
        c.execute("INSERT INTO spread_daily(isin,date,kind,price_pct,ytm,src) "
                  "VALUES(?,?,?,?,?,?)", ("RU000A10OFZ1", FRI, "fixed", 95.0, 11.11, "snap"))
        _bond_day(c, "RU000A10OFZ1", FRI, "TQOB", 1e6, wap=90.0)      # цена дня ≠ снапшота
        _bond_day(c, "RU000A10OFZ2", FRI, "TQOB", 1e6, wap=None, close=97.5)
    out = asyncio.run(get_ofz_asof(date_=SUN))
    assert out["requested"] == SUN and out["date"] == FRI
    a, b = out["items"]["RU000A10OFZ1"], out["items"]["RU000A10OFZ2"]
    assert a["src"] == "snap" and a["ytm"] == 11.11 and a["px"] == 95.0
    assert b["src"] == "reprice" and b["px"] == 97.5
    # пересчёт честный: 8% купон по 97.5 → YTM выше купона, дюрация ~ лет
    assert 8.0 < b["ytm"] < 10.0 and 5.0 < b["tau"] < 8.0
    assert 0 < a["tau"] < 4.0


def test_asof_reprice_uses_accrued_on_that_date(tmp_db, uni, fake_schedules):
    """Пересчёт на прошлую дату: НКД из расписания на дату поставки, а не
    сегодняшний ACCRUEDINT строки (иначе dirty и YTM сдвинуты)."""
    from api.routes import fixed as fx
    from core.valuation import settle_date, accrued_at
    full = fake_schedules["SU26002RMFS2"]
    d = date(2026, 6, 10)
    settle = settle_date(d)
    exp = accrued_at(full["coupons"], settle)
    assert exp is not None and exp > 0
    assert fx._accrued_on(full, settle, 999.0) == pytest.approx(exp)


def test_asof_memo_and_404(tmp_db, uni, fake_schedules):
    from fastapi import HTTPException
    from api.routes import fixed as fx
    with pytest.raises(HTTPException) as e:
        asyncio.run(fx.get_ofz_asof(date_="2026-09-01"))
    assert e.value.status_code == 404
    with tmp_db._connect() as c:
        _bond_day(c, "RU000A10OFZ2", FRI, "TQOB", 1e6, wap=97.5)
    out = asyncio.run(fx.get_ofz_asof(date_=FRI))
    assert set(out["items"]) == {"RU000A10OFZ2"}
    # второй запрос — из памяти, без похода в базу
    with tmp_db._connect() as c:
        c.execute("DELETE FROM bond_day")
    assert asyncio.run(fx.get_ofz_asof(date_=FRI)) is out
