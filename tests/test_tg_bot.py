"""Телеграм-бот: парсер команд, идентичность tg:<id>, Pillow-рендер стакана."""
import asyncio

import pytest

from api.routes.tg import _ALERT_RE, _fmt_alert
from services import tg_users
from services.tg_notify import _caption
from services.tg_render import render_orderbook


# --- парсер /alert ---

def test_alert_re_full():
    m = _ALERT_RE.match("/alert RU000A10AU99 buy yidx >= 250 vol 1000000 rub")
    assert m
    assert m.group("isin") == "RU000A10AU99"
    assert m.group("side") == "buy"
    assert m.group("metric") == "yidx"
    assert m.group("op") == ">="
    assert float(m.group("thr")) == 250.0
    assert float(m.group("vol")) == 1_000_000
    assert m.group("unit") == "rub"


def test_alert_re_minimal_and_case():
    m = _ALERT_RE.match("/alert ru000a10au99 SELL price <= 99.5")
    assert m and m.group("vol") is None
    assert float(m.group("thr")) == 99.5


def test_alert_re_rejects_garbage():
    assert _ALERT_RE.match("/alert RU000A10AU99 hold yidx >= 250") is None
    assert _ALERT_RE.match("/alert SHORT buy yidx >= 250") is None
    assert _ALERT_RE.match("/alert RU000A10AU99 buy vol >= 250") is None


def test_fmt_alert():
    s = _fmt_alert({"id": 7, "isin": "RU000A10AU99", "side": "buy",
                    "metric": "yidx", "op": ">=", "threshold": 250.0,
                    "min_volume": 1e6, "volume_unit": "rub", "status": "active"})
    assert "#7" in s and "RU000A10AU99" in s and "₽" in s


# --- идентичность tg:<id> ---

def test_email_roundtrip():
    email = tg_users.email_for(12345)
    assert email == "tg:12345"
    assert email.startswith(tg_users.EMAIL_PREFIX)


def test_chat_for_email_rejects_foreign():
    assert tg_users.chat_for_email("user@mail.ru") is None
    assert tg_users.chat_for_email("tg:notanumber") is None


# --- caption ---

def test_caption_contents():
    alert = {"id": 3, "isin": "RU000A10AU99", "metric": "yidx", "op": ">=",
             "threshold": 250.0, "side": "buy", "note": "тест"}
    cap = _caption(alert, 1000.0, {"price": 100.15, "volume": 1200}, "Тест-бонд")
    assert "Тест-бонд" in cap and "#3" in cap
    assert "Y-IDX" in cap and "250" in cap
    assert "млн ₽" in cap    # 1200 * 1000 * 100.15/100 ≈ 1.2 млн


# --- рендер ---

def _levels(base, n, step, qty=1000):
    return [{"price": round(base + i * step, 2), "qty": qty * (i + 1),
             "y_idx_bps": 250 - i * 5, "dm_bps": 240 - i * 5,
             "yield_pct": 20.0 + i * 0.1, "g_spread_bps": None}
            for i in range(n)]


def test_render_orderbook_png():
    asks = _levels(100.20, 8, +0.05)
    bids = _levels(100.10, 8, -0.05)
    png = render_orderbook(isin="RU000A10AU99", name="Тест-бонд 001P", kind="floater",
                           bids=bids, asks=asks, hit_price=100.20, hit_side="sell",
                           title="⚡ алерт")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5000


def test_render_empty_sides():
    png = render_orderbook(isin="RU000A10AU99", name=None, kind="fixed",
                           bids=[], asks=_levels(100.2, 3, 0.05))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- фаза 2: initData + REST Mini App ---

import os
import time as _time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.tg_webapp import InitDataError, sign_init_data, validate_init_data


def _init_data(uid=424242, age=0):
    return sign_init_data({
        "auth_date": int(_time.time()) - age,
        "query_id": "AAtest",
        "user": '{"id": %d, "username": "tester", "first_name": "T"}' % uid,
    })


def test_initdata_roundtrip():
    u = validate_init_data(_init_data())
    assert u["tg_user_id"] == 424242 and u["username"] == "tester"


def test_initdata_tampered():
    good = _init_data()
    bad = good.replace("tester", "hacker")
    with pytest.raises(InitDataError):
        validate_init_data(bad)


def test_initdata_stale():
    with pytest.raises(InitDataError):
        validate_init_data(_init_data(age=25 * 3600))


def test_initdata_wrong_token():
    forged = sign_init_data(
        {"auth_date": int(_time.time()), "user": '{"id": 1}'},
        token="1234:WRONG")
    with pytest.raises(InitDataError):
        validate_init_data(forged)


@pytest.fixture()
def tg_client():
    from services import portfolio_db
    from api.routes import tg as tg_route
    portfolio_db.init_db()
    app = FastAPI()
    app.include_router(tg_route.router, prefix="/api/tg")
    from services import tg_users as tgu
    tgu.upsert(424242, 424242, "tester")   # зарегистрирован → allowlist пуст, но пускаем
    yield TestClient(app)
    from services.portfolio_db import _connect, _lock
    with _lock, _connect() as c:
        c.execute("DELETE FROM tg_users WHERE tg_user_id IN (424242, 555)")
        c.execute("DELETE FROM alerts WHERE user_email LIKE 'tg:%'")


def _h(uid=424242):
    return {"Authorization": "tma " + _init_data(uid)}


def test_rest_requires_auth(tg_client):
    assert tg_client.get("/api/tg/alerts").status_code == 401
    assert tg_client.get("/api/tg/alerts",
                         headers={"Authorization": "tma garbage"}).status_code == 401


def test_rest_forbids_stranger(tg_client):
    assert tg_client.get("/api/tg/alerts", headers=_h(uid=555)).status_code == 403


def test_rest_crud_and_rearm(tg_client):
    body = {"isin": "RU000A10AU99", "side": "buy", "metric": "yidx",
            "op": ">=", "threshold": 250, "min_volume": 1e6, "volume_unit": "rub"}
    a = tg_client.post("/api/tg/alerts", json=body, headers=_h()).json()
    assert a["user_email"] == "tg:424242" and a["status"] == "active"

    rows = tg_client.get("/api/tg/alerts", headers=_h()).json()["alerts"]
    assert len(rows) == 1

    # имитируем срабатывание → ре-арм пустым PATCH
    from services import alerts as alerts_svc
    alerts_svc.mark_fired(a["id"], 100.15, 1200)
    r = tg_client.patch(f"/api/tg/alerts/{a['id']}", json={}, headers=_h())
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert r.json()["fired_at"] is None

    assert tg_client.post("/api/tg/mute", json={"muted": True},
                          headers=_h()).json()["muted"] is True
    assert tg_client.get("/api/tg/me", headers=_h()).json()["muted"] is True

    assert tg_client.delete(f"/api/tg/alerts/{a['id']}", headers=_h()).status_code == 200
    assert tg_client.delete(f"/api/tg/alerts/{a['id']}", headers=_h()).status_code == 404


def test_rest_search(tg_client):
    r = tg_client.get("/api/tg/search?q=ОФЗ", headers=_h())
    assert r.status_code == 200
    assert isinstance(r.json()["results"], list)


# --- фаза 3: скринер ---

from services import tg_screener as scr


def test_normalize_params_defaults_and_errors():
    p = scr.normalize_params({"threshold": 300})
    assert p["op"] == ">=" and p["src"] == "ask" and p["threshold"] == 300.0
    with pytest.raises(scr.FilterError):
        scr.normalize_params({"op": "=="})
    with pytest.raises(scr.FilterError):
        scr.normalize_params({"rating_min": "ZZZ"})
    with pytest.raises(scr.FilterError):
        scr.normalize_params({"max_years": -1})


def test_rating_ok():
    assert scr._rating_ok("AAA", "AA-")
    assert scr._rating_ok("AA-", "AA-")
    assert not scr._rating_ok("A+", "AA-")
    assert not scr._rating_ok(None, "AA-")     # нет рейтинга — мимо
    assert scr._rating_ok(None, None)


def _mk_market():
    uni = [
        {"isin": "RU000A0000A1", "name": "Хорошая", "base_rate_type": "KEYRATE",
         "rating": "AA", "maturity_date": "2028-06-01"},
        {"isin": "RU000A0000B2", "name": "Слабый рейтинг", "base_rate_type": "KEYRATE",
         "rating": "BBB", "maturity_date": "2028-06-01"},
        {"isin": "RU000A0000C3", "name": "РУОНИЯ", "base_rate_type": "RUONIA",
         "rating": "AAA", "maturity_date": "2027-01-01"},
        {"isin": "RU000A0000D4", "name": "Стейл", "base_rate_type": "KEYRATE",
         "rating": "AAA", "maturity_date": "2028-06-01"},
    ]
    metrics = {
        "RU000A0000A1": {"yoi_ask": 280.0, "yoi": 275.0, "ask": 100.2, "face_px": 1000.0},
        "RU000A0000B2": {"yoi_ask": 400.0, "yoi": 395.0, "ask": 99.0, "face_px": 1000.0},
        "RU000A0000C3": {"yoi_ask": 180.0, "yoi": 178.0, "ask": 100.0, "face_px": 1000.0},
        "RU000A0000D4": {"yoi_ask": 500.0, "yoi": 490.0, "ask": 98.0, "face_px": 1000.0,
                          "price_stale": True},
    }
    depth = {"RU000A0000A1": {"a": [[100.2, 3000], [100.3, 2000]], "b": []},
             "RU000A0000B2": {"a": [[99.0, 50]], "b": []}}
    return uni, metrics, depth


def test_evaluate_threshold_rating_stale():
    uni, metrics, depth = _mk_market()
    from datetime import date as _d
    matches = scr.evaluate(scr.normalize_params({"threshold": 250, "rating_min": "AA-"}),
                           uni, metrics, depth, today=_d(2026, 8, 11))
    # B2 отсечён рейтингом, C3 порогом, D4 стейлом
    assert [m["isin"] for m in matches] == ["RU000A0000A1"]
    assert matches[0]["depth_rub"] == pytest.approx(100.2 / 100 * 1000 * 3000
                                                    + 100.3 / 100 * 1000 * 2000)


def test_evaluate_depth_and_base():
    uni, metrics, depth = _mk_market()
    from datetime import date as _d
    p = scr.normalize_params({"threshold": 250, "min_depth_rub": 1e6})
    matches = scr.evaluate(p, uni, metrics, depth, today=_d(2026, 8, 11))
    assert [m["isin"] for m in matches] == ["RU000A0000A1"]   # у B2 стакан 49.5к
    p2 = scr.normalize_params({"threshold": 100, "base": "RUONIA"})
    matches2 = scr.evaluate(p2, uni, metrics, depth, today=_d(2026, 8, 11))
    assert [m["isin"] for m in matches2] == ["RU000A0000C3"]


def test_evaluate_max_years():
    uni, metrics, depth = _mk_market()
    from datetime import date as _d
    p = scr.normalize_params({"threshold": 100, "max_years": 1.0})
    matches = scr.evaluate(p, uni, metrics, depth, today=_d(2026, 8, 11))
    assert [m["isin"] for m in matches] == ["RU000A0000C3"]   # погашение 2027-01


def test_fresh_matches_cooldown(tg_client):
    from services import portfolio_db
    f = scr.create(424242, "тест", {"threshold": 250}, cooldown_min=60)
    matches = [{"isin": "RU000A0000A1", "name": "X", "val_bps": 280, "price": 100, "depth_rub": None}]
    assert len(scr.fresh_matches(f["id"], 60, matches)) == 1
    assert len(scr.fresh_matches(f["id"], 60, matches)) == 0   # внутри cooldown
    scr.delete(424242, f["id"])


def test_format_message_caps():
    ms = [{"isin": f"RU000A00000{i}", "name": f"Б{i}", "val_bps": 300 - i,
           "price": 100.0, "depth_rub": 2e6} for i in range(12)]
    txt = scr.format_message("мой фильтр", ms)
    assert "мой фильтр" in txt and "…и ещё 4" in txt
    assert txt.count("•") == scr.MAX_PER_MESSAGE


def test_rest_filters_crud(tg_client):
    body = {"name": "скринер КС", "cooldown_min": 60,
            "params": {"threshold": 260, "base": "KEYRATE", "rating_min": "AA-"}}
    f = tg_client.post("/api/tg/filters", json=body, headers=_h()).json()
    assert f["name"] == "скринер КС" and f["params"]["base"] == "KEYRATE"

    assert len(tg_client.get("/api/tg/filters", headers=_h()).json()["filters"]) == 1

    r = tg_client.patch(f"/api/tg/filters/{f['id']}", json={"enabled": False}, headers=_h())
    assert r.json()["enabled"] is False

    bad = tg_client.post("/api/tg/filters", json={"name": "x", "params": {"op": "=="}}, headers=_h())
    assert bad.status_code == 400

    assert tg_client.delete(f"/api/tg/filters/{f['id']}", headers=_h()).status_code == 200
    assert tg_client.delete(f"/api/tg/filters/{f['id']}", headers=_h()).status_code == 404
