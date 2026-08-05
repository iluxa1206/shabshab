"""Карта подписок WS: опустевший ISIN обязан исчезать из неё.

Регресс, который чинили: unsubscribe/disconnect снимали сокет, но пустой ключ
оставляли. Список для broadcaster'а рос монотонно за аптайм, и каждые 5 секунд
в Alor уходил запрос цены по бумагам, на которые никто не подписан.
"""
import asyncio

import pytest

from api.routes.ws import ConnectionManager


class FakeWS:
    """Достаточно для карты подписок: объект нужен только как ключ/элемент set.
    send_json копит отправленное — им проверяется снапшот цены при подписке."""
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    def __repr__(self):
        return f"FakeWS({self.name})"


@pytest.fixture
def mgr():
    m = ConnectionManager()
    for name in ("a", "b"):
        m.client_subs[FakeWS(name)] = {"market": set(), "orderbook": set()}
    return m


def _socks(mgr):
    return list(mgr.client_subs.keys())


def test_unsubscribe_removes_empty_isin(mgr):
    ws1 = _socks(mgr)[0]
    asyncio.run(mgr.subscribe(ws1, "market", "RU000A100001"))
    assert mgr.active_market_isins() == ["RU000A100001"]

    asyncio.run(mgr.unsubscribe(ws1, "market", "RU000A100001"))
    assert mgr.market_subscriptions == {}
    assert mgr.active_market_isins() == []


def test_isin_survives_while_other_client_listens(mgr):
    ws1, ws2 = _socks(mgr)
    for w in (ws1, ws2):
        asyncio.run(mgr.subscribe(w, "market", "RU000A100001"))
    asyncio.run(mgr.unsubscribe(ws1, "market", "RU000A100001"))
    assert mgr.active_market_isins() == ["RU000A100001"]

    asyncio.run(mgr.unsubscribe(ws2, "market", "RU000A100001"))
    assert mgr.active_market_isins() == []


def test_disconnect_clears_both_channels(mgr):
    ws1 = _socks(mgr)[0]
    asyncio.run(mgr.subscribe(ws1, "market", "RU000A100001"))
    asyncio.run(mgr.subscribe(ws1, "orderbook", "RU000A100002"))

    mgr.disconnect(ws1)
    assert mgr.market_subscriptions == {}
    assert mgr.orderbook_subscriptions == {}
    assert ws1 not in mgr.client_subs


def test_unsubscribe_unknown_isin_is_noop(mgr):
    ws1 = _socks(mgr)[0]
    asyncio.run(mgr.unsubscribe(ws1, "market", "RU000A100009"))
    assert mgr.market_subscriptions == {}


# ── снапшот последней цены ───────────────────────────────────────────────────
# Broadcaster шлёт только ИЗМЕНЕНИЯ цены (иначе фронт на каждый такт заказывал
# reprice: 3.4 запроса/сек на пересчёт одного и того же). Значит подписчик,
# пришедший между движениями, обязан получить последнюю цену при subscribe.

def test_subscribe_sends_last_known_price(mgr):
    ws1, ws2 = _socks(mgr)
    asyncio.run(mgr.subscribe(ws1, "market", "RU000A100001"))
    assert ws1.sent == []                      # цены ещё не было — слать нечего

    asyncio.run(mgr.broadcast_market_data("RU000A100001", {"last_price_pct": 99.5}))
    asyncio.run(mgr.subscribe(ws2, "market", "RU000A100001"))
    assert ws2.sent == [{"channel": "market", "isin": "RU000A100001",
                         "data": {"last_price_pct": 99.5}}]


def test_last_price_dropped_with_last_subscriber(mgr):
    ws1 = _socks(mgr)[0]
    asyncio.run(mgr.subscribe(ws1, "market", "RU000A100001"))
    asyncio.run(mgr.broadcast_market_data("RU000A100001", {"last_price_pct": 99.5}))
    assert mgr.last_market

    asyncio.run(mgr.unsubscribe(ws1, "market", "RU000A100001"))
    assert mgr.last_market == {}               # иначе карта растёт за аптайм
