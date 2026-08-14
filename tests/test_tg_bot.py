"""Телеграм-бот: привязка чата к веб-аккаунту, вебхук, форматирование доставки.
Своей настройки у бота нет — алерты и сигналы заводятся на сайте."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.tg import _fmt_alert, _fmt_event
from services import tg_users
from services.tg_notify import _caption, _signal_text

UID = 424242
SECRET = "test-secret"


@pytest.fixture()
def db():
    from services import portfolio_db
    from services.portfolio_db import _connect, _lock
    portfolio_db.init_db()
    yield
    with _lock, _connect() as c:
        c.execute("DELETE FROM tg_users WHERE tg_user_id IN (?, 555)", (UID,))
        c.execute("DELETE FROM alerts WHERE user_email IN ('tg:424242', 'u@x.ru')")


@pytest.fixture()
def client(db, monkeypatch):
    from api.routes import tg as tg_route
    monkeypatch.setenv("TG_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)   # отправка — no-op
    app = FastAPI()
    app.include_router(tg_route.router, prefix="/api/tg")
    return TestClient(app)


def _update(text, uid=UID, username="tester"):
    return {"message": {"text": text, "from": {"id": uid, "username": username},
                        "chat": {"id": uid, "type": "private"}}}


def _post(client, upd, secret=SECRET):
    return client.post("/api/tg/webhook", json=upd,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


# --- привязка ---

def test_start_creates_pending_request(client):
    assert _post(client, _update("/start")).status_code == 200
    row = tg_users.get(UID)
    assert row["status"] == "pending" and row["email"] is None
    assert row["username"] == "tester"
    assert tg_users.is_allowed(UID) is False
    assert tg_users.email_for(UID) is None


def test_any_message_creates_request(client):
    _post(client, _update("привет", uid=555, username=None))
    assert (tg_users.get(555) or {})["status"] == "pending"


def test_wrong_secret_ignored(client):
    _post(client, _update("/start"), secret="nope")
    assert tg_users.get(UID) is None


def test_approve_binds_account_and_moves_legacy_alerts(client):
    from services import alerts as alerts_svc
    _post(client, _update("/start"))
    legacy = alerts_svc.create(tg_users.legacy_email(UID), isin="RU000A10AU99",
                               side="buy", metric="yidx", op=">=", threshold=250)

    row = tg_users.approve(UID, "U@X.ru", by="admin@x.ru")
    assert row["status"] == "approved" and row["email"] == "u@x.ru"
    assert tg_users.is_allowed(UID) and tg_users.email_for(UID) == "u@x.ru"
    # алерты автономной идентичности переехали на веб-аккаунт
    assert alerts_svc.get(legacy["id"])["user_email"] == "u@x.ru"


def test_chats_for_email_respects_mute_and_status(client):
    _post(client, _update("/start"))
    assert tg_users.chats_for_email("u@x.ru") == []
    tg_users.approve(UID, "u@x.ru", by="admin@x.ru")
    assert [c["tg_user_id"] for c in tg_users.chats_for_email("u@x.ru")] == [UID]
    assert tg_users.has_chats("u@x.ru") is True

    tg_users.set_muted(UID, True)
    assert tg_users.chats_for_email("u@x.ru") == []
    tg_users.set_muted(UID, False)

    tg_users.revoke(UID)
    assert tg_users.chats_for_email("u@x.ru") == []
    assert tg_users.get(UID)["status"] == "rejected"
    assert tg_users.is_allowed(UID) is False


def test_approve_unknown_chat(db):
    assert tg_users.approve(999999, "u@x.ru", by="admin@x.ru") is None
    with pytest.raises(ValueError):
        tg_users.approve(UID, "", by="admin@x.ru")


def test_admin_links_require_admin(client):
    # роутер подключён без cookie-сессии → зависимость require_admin режет
    assert client.get("/api/tg/links").status_code in (401, 403)


# --- форматирование ---

def test_fmt_alert():
    s = _fmt_alert({"id": 7, "isin": "RU000A10AU99", "side": "buy",
                    "metric": "yidx", "op": ">=", "threshold": 250.0,
                    "min_volume": 1e6, "volume_unit": "rub", "status": "active"})
    assert "#7" in s and "RU000A10AU99" in s and "₽" in s


def test_fmt_event():
    s = _fmt_event({"isin": "RU000A10AU99", "name": "Тест-бонд", "val_bps": 284.4,
                    "price": 100.15, "money_rub": 3.2e6,
                    "fired_at": "2026-08-14T10:42:11+00:00"})
    assert "Тест-бонд" in s and "284 бп" in s and "3.2 млн ₽" in s and "10:42" in s


def test_caption_contents():
    alert = {"id": 3, "isin": "RU000A10AU99", "metric": "yidx", "op": ">=",
             "threshold": 250.0, "side": "buy", "note": "тест"}
    cap = _caption(alert, 1000.0, {"price": 100.15, "volume": 1200}, "Тест-бонд")
    assert "Тест-бонд" in cap and "#3" in cap
    assert "R-spread" in cap and "250" in cap
    assert "млн ₽" in cap    # 1200 * 1000 * 100.15/100 ≈ 1.2 млн


def test_signal_text_caps_matches():
    ms = [{"isin": f"RU000A00000{i}", "name": f"Б{i}", "val_bps": 300 - i,
           "price": 100.0, "money_rub": 2e6, "reason": "new"} for i in range(12)]
    txt = _signal_text({"name": "мой фильтр", "side": "bid", "kind": "book",
                        "matches": ms})
    assert "мой фильтр" in txt and "бид" in txt and "…ещё 4" in txt
    assert txt.count("•") == 8


def test_signal_text_block_kind():
    txt = _signal_text({"name": "Крупная сделка", "side": None, "kind": "block",
                        "matches": [{"isin": "RU000A10AU99", "name": "Тест",
                                     "money_rub": 320e6, "price": 100.1,
                                     "reason": "block"}]})
    assert "Крупная сделка" in txt and "320.0 млн ₽" in txt


# --- рендер PNG (Pillow есть только в образе бота — локально пропускаем) ---

def _render():
    pytest.importorskip("PIL", reason="Pillow ставится в образе бота")
    from services.tg_render import render_orderbook
    return render_orderbook


def _levels(base, n, step, qty=1000):
    return [{"price": round(base + i * step, 2), "qty": qty * (i + 1),
             "y_idx_bps": 250 - i * 5, "dm_bps": 240 - i * 5,
             "yield_pct": 20.0 + i * 0.1, "g_spread_bps": None}
            for i in range(n)]


def test_render_orderbook_png():
    asks = _levels(100.20, 8, +0.05)
    bids = _levels(100.10, 8, -0.05)
    png = _render()(isin="RU000A10AU99", name="Тест-бонд 001P", kind="floater",
                    bids=bids, asks=asks, hit_price=100.20, hit_side="sell",
                    title="⚡ алерт")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5000


def test_render_empty_sides():
    png = _render()(isin="RU000A10AU99", name=None, kind="fixed",
                    bids=[], asks=_levels(100.2, 3, 0.05))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
