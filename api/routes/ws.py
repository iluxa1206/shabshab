import asyncio
import json
import logging
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger("api.ws")

class ConnectionManager:
    def __init__(self):
        # mappings of isin -> set of websockets
        self.market_subscriptions: Dict[str, Set[WebSocket]] = {}
        self.orderbook_subscriptions: Dict[str, Set[WebSocket]] = {}
        # mappings of websocket -> what they are subscribed to
        self.client_subs: Dict[WebSocket, dict] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.client_subs[websocket] = {"market": set(), "orderbook": set()}

    def disconnect(self, websocket: WebSocket):
        if websocket in self.client_subs:
            subs = self.client_subs[websocket]
            for isin in subs["market"]:
                if isin in self.market_subscriptions:
                    self.market_subscriptions[isin].discard(websocket)
            for isin in subs["orderbook"]:
                if isin in self.orderbook_subscriptions:
                    self.orderbook_subscriptions[isin].discard(websocket)
            del self.client_subs[websocket]

    async def subscribe(self, websocket: WebSocket, channel: str, isin: str):
        if channel == "market":
            if isin not in self.market_subscriptions:
                self.market_subscriptions[isin] = set()
            self.market_subscriptions[isin].add(websocket)
            self.client_subs[websocket]["market"].add(isin)
        elif channel == "orderbook":
            if isin not in self.orderbook_subscriptions:
                self.orderbook_subscriptions[isin] = set()
            self.orderbook_subscriptions[isin].add(websocket)
            self.client_subs[websocket]["orderbook"].add(isin)

    async def unsubscribe(self, websocket: WebSocket, channel: str, isin: str):
        if channel == "market":
            if isin in self.market_subscriptions:
                self.market_subscriptions[isin].discard(websocket)
            self.client_subs[websocket]["market"].discard(isin)
        elif channel == "orderbook":
            if isin in self.orderbook_subscriptions:
                self.orderbook_subscriptions[isin].discard(websocket)
            self.client_subs[websocket]["orderbook"].discard(isin)

    async def broadcast_market_data(self, isin: str, data: dict):
        if isin in self.market_subscriptions:
            message = {"channel": "market", "isin": isin, "data": data}
            dead_sockets = set()
            for connection in self.market_subscriptions[isin]:
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
                
                if action == "subscribe" and channel and isin:
                    await manager.subscribe(websocket, channel, isin)
                    await websocket.send_json({"status": "subscribed", "channel": channel, "isin": isin})
                elif action == "unsubscribe" and channel and isin:
                    await manager.unsubscribe(websocket, channel, isin)
                    await websocket.send_json({"status": "unsubscribed", "channel": channel, "isin": isin})
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON format"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
