"""Разовая регистрация вебхука Telegram-бота.

⚠️ НА ПРОД-VPS ВЕБХУК НЕ РАБОТАЕТ. Telegram не может открыть к нам входящее
соединение (getWebhookInfo отдаёт «Connection timed out»), а активный вебхук
вдобавок блокирует getUpdates — то есть ЛОМАЕТ рабочий поллинг (TG_POLLING=1,
services/tg_poll.py). 21.08.2026 бот так молчал час, 4 команды висели в очереди.
Скрипт оставлен для окружений, где входящее соединение проходит; на проде
пользоваться им не нужно — при TG_POLLING=1 он предупредит и потребует --force.

Использование: python scripts/tg_set_webhook.py [url] [--force]
url по умолчанию — env TG_WEBHOOK_URL. Секрет — env TG_WEBHOOK_SECRET."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import telegram  # noqa: E402


async def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    url = args[0] if args else os.getenv("TG_WEBHOOK_URL")
    secret = os.getenv("TG_WEBHOOK_SECRET")
    if not telegram.enabled():
        sys.exit("TG_BOT_TOKEN не задан")
    # ЗАЩИТА ОТ ВЫСТРЕЛА В НОГУ: при включённом поллинге вебхук не «дополняет»
    # его, а ГЛУШИТ — Bot API отдаёт на getUpdates 409 Conflict, и бот перестаёт
    # принимать команды вовсе (вебхук на этом VPS тоже не доходит).
    from services import tg_poll
    if tg_poll.enabled() and not force:
        sys.exit("TG_POLLING=1 — бот принимает команды поллингом, и вебхук его "
                 "СЛОМАЕТ (getUpdates → 409 Conflict).\n"
                 "Если это точно нужно: снимите TG_POLLING или запустите с --force.")
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
        {"command": "signals", "description": "последние сигналы"},
        {"command": "custom", "description": "свои эмодзи для маркеров"},
        {"command": "chats", "description": "каналы для доставки"},
        {"command": "mute", "description": "пауза доставки"},
        {"command": "unmute", "description": "включить доставку"},
        {"command": "status", "description": "состояние привязки"},
        {"command": "help", "description": "справка"},
    ]}))


if __name__ == "__main__":
    asyncio.run(main())
