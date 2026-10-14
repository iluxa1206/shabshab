"""Состояние канала Telegram и внешняя тревога (09–12.10.2026: прокси выпал из
подписки, а тревоги шли через тот же Telegram — три дня тишины)."""
import asyncio

import httpx
import pytest

from services import telegram


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setattr(telegram, "_state", {"last_ok": 0.0, "last_fail": 0.0, "fails": 0})
    monkeypatch.setattr(telegram, "_STARTED", 1000.0)
    monkeypatch.setattr(telegram, "_clients", {})
    monkeypatch.setattr(telegram, "DOWN_AFTER_SEC", 300.0)


def test_off_without_token(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN")
    assert telegram.channel_status(5000.0)["state"] == "off"


def test_fresh_process_is_ok_until_threshold():
    telegram._state["fails"] = 5          # отказы есть, но процесс молод
    assert telegram.channel_status(1100.0)["state"] == "ok"
    assert telegram.channel_status(1400.0)["state"] == "down"


def test_success_resets_down():
    telegram._state.update(fails=9, last_ok=1000.0)
    assert telegram.channel_status(5000.0)["state"] == "down"
    telegram._mark(True)
    import time
    assert telegram.channel_status(time.time())["state"] == "ok"


def test_single_failure_is_not_down():
    telegram._state["fails"] = 2          # порог — 3 исчерпанных вызова подряд
    assert telegram.channel_status(9999.0)["state"] == "ok"


def test_exhausted_retries_count_as_failure(monkeypatch):
    async def boom(*a, **k):
        raise httpx.ReadTimeout("")
    async def nosleep(*a, **k):
        return None
    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(telegram.asyncio, "sleep", nosleep)
    assert asyncio.run(telegram.call("getMe")) is None
    assert telegram._state["fails"] == 1


def test_api_level_error_means_channel_alive(monkeypatch):
    class R:
        status_code = 200
        def json(self):
            return {"ok": False, "description": "Forbidden"}
    async def ok(*a, **k):
        return R()
    monkeypatch.setattr(httpx.AsyncClient, "post", ok)
    telegram._state["fails"] = 4
    assert asyncio.run(telegram.call("sendMessage", {})) is None
    assert telegram._state["fails"] == 0   # канал ответил, ошибка прикладная


def test_health_exposes_state(monkeypatch):
    from api.routes.health import get_health
    monkeypatch.setattr(telegram, "channel_status", lambda: {"state": "down", "down_sec": 999})
    assert asyncio.run(get_health()).telegram == "down"


def test_watchdog_posts_external_alert_once(monkeypatch):
    import api.main as m
    sent = []
    async def post(text):
        sent.append(text)
        return True
    monkeypatch.setattr(m, "_post_external_alert", post)
    monkeypatch.setattr(m, "_tg_down_alerted", {})
    monkeypatch.setattr(telegram, "channel_status", lambda: {"state": "down", "down_sec": 700})
    calls = {"n": 0}
    async def sleep(sec):
        calls["n"] += 1
        if calls["n"] >= 4:                 # 1-й — стартовая пауза, дальше два витка
            raise asyncio.CancelledError
    monkeypatch.setattr(m.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m.telegram_watchdog(period_sec=1))
    assert len(sent) == 1 and "11 мин" in sent[0]      # второй виток — без повтора
