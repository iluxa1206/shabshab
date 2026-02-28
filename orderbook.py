import asyncio
import json
import uuid
from auth import get_access_token, REFRESH_TOKEN, BASE_API
import aiohttp
from readisins import read_isins_from_file

ALOR_WS_URLS = {
    "prod": "wss://api.alor.ru/ws",
    "dev": "wss://apidev.alor.ru/ws",
}


async def alor_ws_order_book_stream(
    access_token: str,
    exchange: str,
    isins: list[str],
    depth: int = 20,  # Глубина стакана
    env: str = "prod",
    frequency: int = 250,
    on_order_book=None,
    large_order_threshold: float = 1000,  # Минимальный объем для "крупной" заявки
):
    url = ALOR_WS_URLS[env]
    api_base = f"{BASE_API}/md/v2/Securities/{exchange}"

    async def _fetch_shortnames() -> dict[str, str]:
        """Получает shortName для каждого ISIN, чтобы подписывать вывод."""
        headers = {"Authorization": f"Bearer {access_token}"}
        names: dict[str, str] = {}
        async with aiohttp.ClientSession(headers=headers) as session:
            for isin in isins:
                try:
                    async with session.get(f"{api_base}/{isin}") as resp:
                        if resp.status != 200:
                            continue
                        payload = await resp.json()
                        short = payload.get("shortname") # исправлено с "shortName"
                        if short:
                            names[isin] = short
                except Exception:
                    # При ошибке просто пропустим, оставляя ISIN как подпись
                    continue
        return names
    
    def _render_order_book(order_data: dict) -> None:
        # Берём только крупные заявки по порогу
        bids = [
            b for b in order_data["bids"]
            if (b["qty"] or 0) >= large_order_threshold
        ]
        asks = [
            a for a in order_data["asks"]
            if (a["qty"] or 0) >= large_order_threshold
        ]

        # Сортируем — биды по убыванию цены, офера по возрастанию
        bids = sorted(bids, key=lambda x: x["price"] or 0, reverse=True)
        asks = sorted(asks, key=lambda x: x["price"] or 0)

        def _fmt_price(val: float | None) -> str:
            return f"{val:>12.4f}" if val is not None else " " * 12

        def _fmt_qty(val: float | None) -> str:
            return f"{val:>10}" if val is not None else " " * 10

        title = shortnames.get(order_data["isin"], order_data["isin"])
        print(f"\nКрупные заявки {title} ({order_data['isin']}), >= {large_order_threshold:.0f} шт")
        print(f"{'BID_QTY':>10} {'BID_PRICE':>12} | {'ASK_PRICE':>12} {'ASK_QTY':>10}")

        rows = max(len(bids), len(asks), 1)  # хотя бы одна строка, чтобы таблица не ломалась
        for i in range(rows):
            bid = bids[i] if i < len(bids) else None
            ask = asks[i] if i < len(asks) else None
            line = (
                f"{_fmt_qty(bid.get('qty') if bid else None)} "
                f"{_fmt_price(bid.get('price') if bid else None)} | "
                f"{_fmt_price(ask.get('price') if ask else None)} "
                f"{_fmt_qty(ask.get('qty') if ask else None)}"
            )
            print(line)
        print()


    async def _stream_single_isin(isin: str):
        """Подписка и обработка стакана для конкретного ISIN."""
        guid = str(uuid.uuid4())
        sub_msg = {
            "opcode": "OrderBookGetAndSubscribe",
            "exchange": exchange,
            "code": isin,
            "depth": depth,
            "format": "Slim",
            "frequency": frequency,
            "guid": guid,
            "token": access_token,
        }

        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url) as ws:
                        print(f"WS подключен: {url} (isin={isin})")
                        print("Отправляем подписку:", sub_msg)
                        await ws.send_str(json.dumps(sub_msg))

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                except json.JSONDecodeError:
                                    print("Не смог распарсить JSON:", msg.data)
                                    continue

                                if "httpCode" in data:
                                    print("Служебное сообщение:", data)
                                    continue

                                payload = data.get("data")
                                if not payload:
                                    print("Сообщение без 'data':", data)
                                    continue

                                bids = [
                                    {
                                        "price": entry.get("p"),
                                        "qty": entry.get("v"),
                                        "ytm": entry.get("y"),
                                    }
                                    for entry in (payload.get("b") or [])
                                ]
                                asks = [
                                    {
                                        "price": entry.get("p"),
                                        "qty": entry.get("v"),
                                        "ytm": entry.get("y"),
                                    }
                                    for entry in (payload.get("a") or [])
                                ]

                                order_data = {
                                    "isin": isin,
                                    "exchange": exchange,
                                    "guid": data.get("guid"),
                                    "bids": bids,
                                    "asks": asks,
                                    "large_bids": [b for b in bids if (b["qty"] or 0) >= large_order_threshold],
                                    "large_asks": [a for a in asks if (a["qty"] or 0) >= large_order_threshold],
                                    "raw": data,
                                }

                                if on_order_book:
                                    if asyncio.iscoroutinefunction(on_order_book):
                                        await on_order_book(order_data)
                                    else:
                                        on_order_book(order_data)
                                else:
                                    _render_order_book(order_data)

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print("WS ошибка:", ws.exception())
                                break
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                                print("WS закрыт")
                                break
            except Exception as e:
                print(f"Ошибка WebSocket для {isin}: {e}. Повтор через 5 секунд.")
                await asyncio.sleep(5)  # Задержка перед переподключением

    shortnames = await _fetch_shortnames()
    tasks = [asyncio.create_task(_stream_single_isin(isin)) for isin in isins]
    await asyncio.gather(*tasks)


async def main():
    access_token = get_access_token(REFRESH_TOKEN)

    # Пример с несколькими ISIN
    isins = read_isins_from_file()

    await alor_ws_order_book_stream(
        access_token=access_token,
        exchange="MOEX",  # MOEX или SPBX
        isins=isins,
        depth=20,
        env="prod",
        frequency=5000,
    )


if __name__ == "__main__":
    asyncio.run(main())
