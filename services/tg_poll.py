"""Long polling Bot API — боевой способ получать команды бота.

Вебхук на прод-VPS не работает: Telegram не может открыть к нам соединение
(getWebhookInfo отдаёт «Connection timed out», апдейты копятся в очереди), и
починить это с нашей стороны нечем — соединение входящее, туннель на него не
натянешь. Поллинг же исходящий: getUpdates уходит тем же путём, что и отправка
сообщений, то есть через TG_PROXY (см. services/telegram.py).

Смещение (offset) держим в памяти: Telegram повторяет неподтверждённые
апдейты, поэтому после рестарта максимум разберём заново последнюю пачку —
все команды бота идемпотентны (/start заводит заявку через ON CONFLICT).

Включается env TG_POLLING=1. Вебхук при старте снимается: одновременно с ним
Bot API отдаёт на getUpdates 409 Conflict.
"""
import asyncio
import logging
import os

from services import telegram

logger = logging.getLogger(__name__)

# Long poll: соединение висит POLL_TIMEOUT секунд и рвётся, как только пришёл
# апдейт. Клиентский таймаут держим заведомо больше серверного, иначе httpx
# рубит живое ожидание и апдейты приезжают рывками.
POLL_TIMEOUT = int(os.getenv("TG_POLL_TIMEOUT", "25"))
_HTTP_TIMEOUT = POLL_TIMEOUT + 15
_ERR_BACKOFF = 5.0


def enabled() -> bool:
    return (os.getenv("TG_POLLING") or "").strip().lower() in ("1", "true", "yes", "on")


async def tg_poll_worker() -> None:
    if not enabled() or not telegram.enabled():
        return
    from api.routes.tg import process_update

    logger.info("tg_poll worker started (timeout=%ss)", POLL_TIMEOUT)
    # снимаем вебхук; drop_pending_updates НЕ ставим — накопленные заявки на
    # доступ отдаём поллеру, они и есть то, чего ждёт админ
    await telegram.call("deleteWebhook", {"drop_pending_updates": False})

    offset = None
    while True:
        try:
            payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = await telegram.call("getUpdates", payload,
                                          timeout=_HTTP_TIMEOUT)
            if updates is None:            # сеть/прокси легли — telegram.call уже
                await asyncio.sleep(_ERR_BACKOFF)   # отретраил и залогировал
                continue
            for u in updates:
                offset = int(u["update_id"]) + 1    # подтверждаем ДО разбора:
                try:                                # битый апдейт не должен
                    await process_update(u)         # крутиться вечно
                except Exception as e:
                    logger.warning("tg_poll: апдейт %s не разобран: %s",
                                   u.get("update_id"), e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("tg_poll error: %s", e)
            await asyncio.sleep(_ERR_BACKOFF)
