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
