"""Батч-снимок стаканов Alor по списку ISIN одним WS-заходом.

Тот же приём, что core.last_prices.get_last_prices_dict (подписка пачкой →
собираем первый пуш на бумагу → закрываем сокет), только opcode
OrderBookGetAndSubscribe: per-isin HTTP (/md/v2/orderbooks) на 540 бумаг — это
540 запросов, WS-пачка укладывается в один сокет и несколько секунд.

Персистентный alor_ws (services/alor_ws.py) здесь не переиспользуем: он держит
подписки под открытые карточки фронта и стримит уровни с метриками, а тут нужен
редкий батч-снимок всего рынка без расчёта."""
import asyncio
import json
import logging
from typing import Dict, List

import aiohttp

from auth import BASE_API

logger = logging.getLogger(__name__)

_WS_URL = BASE_API.replace("https://", "wss://") + "/ws"
_WS_TIMEOUT = aiohttp.ClientTimeout(sock_connect=5, sock_read=15)


def _levels(raw) -> list:
    out = []
    for e in raw or []:
        p, q = e.get("price"), e.get("volume")
        if p is None or q is None:
            continue
        out.append([float(p), float(q)])
    return out


async def get_orderbooks_dict(access_token: str, exchange: str, isins: List[str],
                              depth: int = 20, timeout: float = 8.0) -> Dict[str, dict]:
    """{isin: {"b": [[price_pct, qty], ...], "a": [...]}} — снимок стакана на бумагу.

    Пустой стакан (неликвид без заявок) тоже попадает в результат: пустые списки —
    это факт «заявок нет», а не «данные не пришли». Бумаги, по которым Alor не
    ответил за timeout, в словаре отсутствуют — вызывающий сохраняет прошлый снимок.
    """
    result: Dict[str, dict] = {}
    if not isins:
        return result
    guid_isin = {f"obb-{i}": isin for i, isin in enumerate(isins)}

    async with aiohttp.ClientSession(timeout=_WS_TIMEOUT) as session:
        try:
            async with session.ws_connect(_WS_URL, heartbeat=20) as ws:
                for guid, isin in guid_isin.items():
                    await ws.send_json({
                        "opcode": "OrderBookGetAndSubscribe", "code": isin,
                        "exchange": exchange, "depth": depth, "format": "Simple",
                        "frequency": 5000, "guid": guid, "token": access_token})
                loop = asyncio.get_event_loop()
                start = loop.time()
                while len(result) < len(guid_isin):
                    if loop.time() - start > timeout:
                        break
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
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
                    isin = guid_isin.get(guid)
                    if not isin or data is None:
                        continue
                    # первый пуш на бумагу — снимок; последующие обновления
                    # игнорируем, иначе цикл не сойдётся на ликвиде
                    if isin in result:
                        continue
                    result[isin] = {"b": _levels(data.get("bids")),
                                    "a": _levels(data.get("asks"))}
        except Exception as e:
            logger.warning("Alor orderbook WS batch failed (%d isins): %s: %s",
                           len(guid_isin), type(e).__name__, e)
    return result
