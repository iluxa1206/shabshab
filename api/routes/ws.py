import asyncio
import json
import logging
import re
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger("api.ws")

# Подписки не были ограничены: любой авторизованный мог накачать broadcaster
# произвольными строками-«isin» (каждая гонится в Alor каждые 5с) без лимита.
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_MAX_SUBS_PER_CLIENT = 300
_VALID_CHANNELS = ("market", "orderbook")

class ConnectionManager:
    def __init__(self):
        # mappings of isin -> set of websockets
        self.market_subscriptions: Dict[str, Set[WebSocket]] = {}
        self.orderbook_subscriptions: Dict[str, Set[WebSocket]] = {}
        # mappings of websocket -> what they are subscribed to
        self.client_subs: Dict[WebSocket, dict] = {}
        # последняя разосланная цена по ISIN — снапшот для нового подписчика.
        # Broadcaster шлёт только ИЗМЕНЕНИЯ, без этого свежая вкладка ждала бы
        # первого движения цены (или heartbeat'а) с пустой строкой.
        self.last_market: Dict[str, dict] = {}
        # сокеты с wildcard-подпиской market:* — получают патчи ВСЕХ бумаг
        # (вся таблица живая, а не только избранное)
        self.market_firehose: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.client_subs[websocket] = {"market": set(), "orderbook": set()}

    def _drop(self, channel: str, isin: str, websocket: WebSocket):
        """Снимает сокет с ISIN и УБИРАЕТ опустевший ключ. Без удаления ключа
        карта подписок росла вечно: broadcaster гнал в Alor запрос цены по
        каждому ISIN, на который хоть кто-то когда-то подписывался за аптайм."""
        subs = self.market_subscriptions if channel == "market" else self.orderbook_subscriptions
        socks = subs.get(isin)
        if socks is None:
            return
        socks.discard(websocket)
        if not socks:
            del subs[isin]
            if channel == "market":
                self.last_market.pop(isin, None)   # никто не смотрит — снапшот не нужен

    def disconnect(self, websocket: WebSocket):
        self.market_firehose.discard(websocket)
        if websocket in self.client_subs:
            subs = self.client_subs[websocket]
            for isin in subs["market"]:
                self._drop("market", isin, websocket)
            for isin in subs["orderbook"]:
                self._drop("orderbook", isin, websocket)
            del self.client_subs[websocket]

    def has_market_audience(self, isin: str) -> bool:
        """Есть кому слать патч бумаги: точечная подписка или wildcard."""
        return bool(self.market_firehose or self.market_subscriptions.get(isin))

    async def subscribe(self, websocket: WebSocket, channel: str, isin: str):
        if channel == "market" and isin == "*":
            self.market_firehose.add(websocket)
            # снапшот всего, что уже известно — вкладка стартует с полной
            # картиной, а не ждёт первого движения каждой бумаги
            for i, snap in list(self.last_market.items()):
                try:
                    await websocket.send_json({"channel": "market", "isin": i, "data": snap})
                except Exception:
                    self.disconnect(websocket)
                    return
            return
        if channel == "market":
            if isin not in self.market_subscriptions:
                self.market_subscriptions[isin] = set()
            self.market_subscriptions[isin].add(websocket)
            self.client_subs[websocket]["market"].add(isin)
            snap = self.last_market.get(isin)
            if snap is not None:
                try:
                    await websocket.send_json({"channel": "market", "isin": isin, "data": snap})
                except Exception:
                    self.disconnect(websocket)
        elif channel == "orderbook":
            if isin not in self.orderbook_subscriptions:
                self.orderbook_subscriptions[isin] = set()
            self.orderbook_subscriptions[isin].add(websocket)
            self.client_subs[websocket]["orderbook"].add(isin)

    async def unsubscribe(self, websocket: WebSocket, channel: str, isin: str):
        if channel == "market":
            self._drop("market", isin, websocket)
            self.client_subs[websocket]["market"].discard(isin)
        elif channel == "orderbook":
            self._drop("orderbook", isin, websocket)
            self.client_subs[websocket]["orderbook"].discard(isin)

    def active_market_isins(self) -> list:
        """ISIN с ЖИВЫМИ подписчиками — вход broadcaster'а."""
        return [i for i, socks in self.market_subscriptions.items() if socks]

    async def broadcast_market_data(self, isin: str, data: dict):
        targets = set(self.market_subscriptions.get(isin) or ()) | self.market_firehose
        if targets:
            # снапшот МЕРДЖИМ, а не заменяем: пуши бывают частичными (сделка
            # несёт цену и средневзвес, котировка — цену и верх стакана), и
            # заменой новый подписчик получал бы обрывок вместо всей строки
            self.last_market[isin] = {**self.last_market.get(isin, {}), **data}
            message = {"channel": "market", "isin": isin, "data": data}
            dead_sockets = set()
            for connection in targets:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_sockets.add(connection)
            for ws in dead_sockets:
                self.disconnect(ws)

    async def broadcast_orderbook(self, isin: str, data: dict):
        if isin in self.orderbook_subscriptions:
            message = {"channel": "orderbook", "isin": isin, "data": data}
            dead_sockets = set()
            for connection in self.orderbook_subscriptions[isin]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_sockets.add(connection)
            for ws in dead_sockets:
                self.disconnect(ws)


manager = ConnectionManager()


@router.websocket("/market")
async def websocket_market_endpoint(websocket: WebSocket):
    # Закрываем WS без валидной сессии (cookie уходит на хендшейке, same-origin).
    from api.routes.auth import user_from_websocket
    if not user_from_websocket(websocket):
        await websocket.close(code=1008)  # policy violation
        return
    await manager.connect(websocket)
    try:
        while True:
            # Wait for client commands like {"action": "subscribe", "channel": "market", "isin": "RU000A108447"}
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                action = payload.get("action")
                channel = payload.get("channel")
                isin = payload.get("isin")
                
                isin = (isin or "").strip().upper() if isinstance(isin, str) else None
                # wildcard: вся таблица одним сообщением, вне лимита точечных подписок
                if channel == "market" and isin == "*":
                    if action == "subscribe":
                        await manager.subscribe(websocket, "market", "*")
                        await websocket.send_json({"status": "subscribed", "channel": "market", "isin": "*"})
                    elif action == "unsubscribe":
                        manager.market_firehose.discard(websocket)
                        await websocket.send_json({"status": "unsubscribed", "channel": "market", "isin": "*"})
                    continue
                if action == "subscribe" and channel in _VALID_CHANNELS and isin:
                    if not _ISIN_RE.fullmatch(isin):
                        await websocket.send_json({"error": f"invalid isin: {isin[:20]}"})
                        continue
                    subs = manager.client_subs.get(websocket, {})
                    n_subs = sum(len(s) for s in subs.values())
                    if n_subs >= _MAX_SUBS_PER_CLIENT:
                        await websocket.send_json({"error": "subscription limit reached"})
                        continue
                    await manager.subscribe(websocket, channel, isin)
                    await websocket.send_json({"status": "subscribed", "channel": channel, "isin": isin})
                elif action == "unsubscribe" and channel in _VALID_CHANNELS and isin:
                    await manager.unsubscribe(websocket, channel, isin)
                    await websocket.send_json({"status": "unsubscribed", "channel": channel, "isin": isin})
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON format"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
