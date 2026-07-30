"""Alor WebSocket — реал-тайм стакан. Персистентный WS к wss://api.alor.ru/ws,
подписка OrderBookGetAndSubscribe на isin'ы, у которых есть фронт-подписчики
(manager.orderbook_subscriptions). На каждый пуш — reprice уровней (memo
price→metrics, тик повторяет цены → считаем только новые) → broadcast_orderbook.

Дополняет HTTP-поллинг /api/orderbook (фронт держит его фолбэком): если WS ляжет,
стакан продолжит обновляться поллингом."""
import asyncio
import json
import logging
import time

import aiohttp

from auth import get_access_token, REFRESH_TOKEN, BASE_API

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
_DEPTH = 50            # глубина подписки (фронт нарезает под свой селектор)
_FREQ_MS = 800         # серверный троттл Alor: не чаще раза в 800мс на бумагу
_CTX_TTL = 300         # пересборка reprice-контекста per isin, сек
_RECONCILE_SEC = 2.0   # период сверки подписок с фронтом


class _Sub:
    __slots__ = ("guid", "kind", "metrics_fn", "face", "ctx_ts", "memo")

    def __init__(self, guid):
        self.guid = guid
        self.kind = None
        self.metrics_fn = None
        self.face = None
        self.ctx_ts = 0.0
        self.memo = {}     # price -> {yield_pct, dm_bps, g_spread_bps}


async def _detect_kind(isin: str) -> str:
    from services.market_data import market_cache
    fx = market_cache.get("fixed_universe") or []
    return "fixed" if any(u.get("isin") == isin for u in fx) else "floater"


async def _ensure_ctx(sub: _Sub, isin: str) -> None:
    now = time.time()
    if sub.metrics_fn is not None and now - sub.ctx_ts < _CTX_TTL:
        return
    from services.orderbook_svc import build_metrics_fn
    if sub.kind is None:
        sub.kind = await _detect_kind(isin)
    try:
        sub.metrics_fn, _cd, sub.face = await build_metrics_fn(isin, sub.kind)
        sub.ctx_ts = now
        sub.memo = {}     # ctx пересобран → memo невалиден
    except Exception as e:
        logger.debug(f"alor_ws ctx {isin}: {e}")


def _levels(sub: _Sub, raw) -> list:
    out = []
    for e in raw or []:
        p, q = e.get("price"), e.get("volume")
        if p is None:
            continue
        m = sub.memo.get(p)
        if m is None:
            if sub.metrics_fn:
                try:
                    m = sub.metrics_fn(p)
                except Exception:
                    m = {}
            else:
                m = {}
            sub.memo[p] = m or {}
            m = sub.memo[p]
        out.append({"price_pct": p, "quantity": q, "yield_pct": m.get("yield_pct"),
                    "dm_bps": m.get("dm_bps"), "y_idx_bps": m.get("y_idx_bps"),
                    "g_spread_bps": m.get("g_spread_bps")})
    return out


async def alor_orderbook_ws():
    from api.routes import ws as wsmod
    manager = wsmod.manager
    backoff = 1
    while True:
        token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(_WS_URL, heartbeat=20, timeout=15) as ws:
                    backoff = 1
                    subs = {}        # isin -> _Sub
                    guid_isin = {}   # guid -> isin
                    seq = 0
                    last_reconcile = 0.0
                    while True:
                        now = time.time()
                        # сверка подписок с фронтом
                        if now - last_reconcile >= _RECONCILE_SEC:
                            last_reconcile = now
                            want = {i for i, socks in manager.orderbook_subscriptions.items() if socks}
                            for isin in want - set(subs):
                                seq += 1
                                guid = f"ob-{isin}-{seq}"
                                subs[isin] = _Sub(guid)
                                guid_isin[guid] = isin
                                await ws.send_json({
                                    "opcode": "OrderBookGetAndSubscribe", "code": isin,
                                    "exchange": "MOEX", "depth": _DEPTH, "format": "Simple",
                                    "frequency": _FREQ_MS, "guid": guid, "token": token})
                            for isin in set(subs) - want:
                                s = subs.pop(isin)
                                guid_isin.pop(s.guid, None)
                                try:
                                    await ws.send_json({"opcode": "unsubscribe",
                                                        "token": token, "guid": s.guid})
                                except Exception:
                                    pass
                        # читаем сообщение (короткий таймаут → сверка отзывчива)
                        try:
                            msg = await ws.receive(timeout=_RECONCILE_SEC)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                        aiohttp.WSMsgType.CLOSING):
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue
                        data, guid = payload.get("data"), payload.get("guid")
                        if not data or guid not in guid_isin:
                            continue
                        isin = guid_isin[guid]
                        sub = subs.get(isin)
                        if not sub:
                            continue
                        await _ensure_ctx(sub, isin)
                        out = {
                            "orderbook": {"bids": _levels(sub, data.get("bids")),
                                          "asks": _levels(sub, data.get("asks"))},
                            "pricing_status": "SUCCESS", "warnings": [], "src": "ws",
                        }
                        await manager.broadcast_orderbook(isin, out)
        except Exception as e:
            logger.warning(f"alor_ws error: {e}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
