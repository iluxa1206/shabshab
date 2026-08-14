"""Разовая регистрация вебхука Telegram-бота.
Использование: python scripts/tg_set_webhook.py [url]
url по умолчанию — env TG_WEBHOOK_URL. Секрет — env TG_WEBHOOK_SECRET."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import telegram  # noqa: E402


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TG_WEBHOOK_URL")
    secret = os.getenv("TG_WEBHOOK_SECRET")
    if not telegram.enabled():
        sys.exit("TG_BOT_TOKEN не задан")
    if not url or not secret:
        sys.exit("Нужны TG_WEBHOOK_URL (или аргумент) и TG_WEBHOOK_SECRET")
    res = await telegram.call("setWebhook", {
        "url": url, "secret_token": secret,
        "allowed_updates": ["message"], "drop_pending_updates": True})
    print("setWebhook:", res)
    info = await telegram.call("getWebhookInfo")
    print("getWebhookInfo:", info)
    # Mini App снесён: меню-кнопка возвращается к списку команд, иначе у старых
    # чатов она осталась бы указывать на удалённую страницу
    print("setChatMenuButton:", await telegram.call(
        "setChatMenuButton", {"menu_button": {"type": "commands"}}))
    print("setMyCommands:", await telegram.call("setMyCommands", {"commands": [
        {"command": "alerts", "description": "мои алерты"},
        {"command": "signals", "description": "последние сигналы"},
        {"command": "mute", "description": "пауза доставки"},
        {"command": "unmute", "description": "включить доставку"},
        {"command": "status", "description": "состояние привязки"},
        {"command": "help", "description": "справка"},
    ]}))


if __name__ == "__main__":
    asyncio.run(main())
