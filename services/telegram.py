"""Тонкий async-клиент Telegram Bot API (без aiogram: команд мало, настройка
живёт на сайте). Ретраи на 429 с уважением retry_after и на 5xx.
Токен из env TG_BOT_TOKEN; без него клиент выключен (enabled() == False) —
все send_* тихо no-op, чтобы дев без бота не сыпал ошибками.

TG_PROXY (socks5://user:pass@host:port или http://...) уводит ТОЛЬКО вызовы
Bot API в прокси — биржевой трафик (Alor/MOEX/ЦБ) идёт напрямую, вебхук тоже
не затронут (он входящий). На проде это сайдкар xray с Reality-туннелем,
см. docker-compose.prod.yml. Пустой TG_PROXY = прямое соединение."""
import asyncio
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_RETRIES = 3


def _token() -> Optional[str]:
    return os.getenv("TG_BOT_TOKEN") or None


def enabled() -> bool:
    return _token() is not None


def _proxy() -> Optional[str]:
    return (os.getenv("TG_PROXY") or "").strip() or None


async def call(method: str, payload: Optional[dict] = None,
               files: Optional[dict] = None, timeout: float = 30.0) -> Optional[dict]:
    """POST {method} → result из конверта Bot API. None при выключенном клиенте
    или исчерпанных ретраях (доставка best-effort, вызывающий не падает)."""
    token = _token()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=timeout, proxy=_proxy()) as cli:
        for attempt in range(_RETRIES):
            try:
                if files:
                    r = await cli.post(url, data=payload or {}, files=files)
                else:
                    r = await cli.post(url, json=payload or {})
                if r.status_code == 429:
                    retry_after = (r.json().get("parameters") or {}).get("retry_after", 3)
                    await asyncio.sleep(float(retry_after) + 0.5)
                    continue
                if r.status_code >= 500:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                body = r.json()
                if not body.get("ok"):
                    logger.warning("tg %s error: %s", method, body.get("description"))
                    return None
                return body.get("result")
            except httpx.ConnectError as e:
                # Без прокси: на прод-VPS имя api.telegram.org резолвится
                # провайдерским DNS только в IPv6 (маршрута нет), а часть
                # IPv4-пула Telegram блокируется — лечится extra_hosts/TG_API_IP.
                # С прокси сюда же приходит падение сайдкара tgproxy.
                logger.warning("tg %s connect error (attempt %d): %s — проверь %s",
                               method, attempt + 1, e,
                               "сайдкар tgproxy (TG_PROXY)" if _proxy()
                               else "TG_API_IP/extra_hosts")
                await asyncio.sleep(1.5 * (attempt + 1))
            except httpx.HTTPError as e:
                logger.warning("tg %s http error (attempt %d): %s", method, attempt + 1, e)
                await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def send_message(chat_id: int, text: str, *,
                       parse_mode: Optional[str] = "HTML",
                       disable_notification: bool = False,
                       reply_markup: Optional[dict] = None) -> Optional[dict]:
    payload = {"chat_id": chat_id, "text": text,
               "disable_notification": disable_notification,
               "link_preview_options": {"is_disabled": True}}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await call("sendMessage", payload)


async def send_photo(chat_id: int, png: bytes, caption: str = "", *,
                     parse_mode: Optional[str] = "HTML") -> Optional[dict]:
    payload = {"chat_id": chat_id, "caption": caption}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return await call("sendPhoto", payload,
                      files={"photo": ("orderbook.png", png, "image/png")})


async def set_webhook(url: str, secret_token: str) -> Optional[dict]:
    return await call("setWebhook", {
        "url": url, "secret_token": secret_token,
        "allowed_updates": ["message"], "drop_pending_updates": True})
