"""Webhook Telegram-бота. Вне require_user: защита — секрет-заголовок
X-Telegram-Bot-Api-Secret-Token (env TG_WEBHOOK_SECRET) + allowlist tg_users.
Идентичность бота автономна: алерты в общей таблице alerts под
user_email 'tg:<tg_user_id>' (см. services/tg_users.py).

Команды фазы 1 (CRUD руками, Mini App — фаза 2):
  /alert <ISIN> <buy|sell> <метрика> <=|>= <порог> [vol <N> [rub]]
  /alerts, /del <id>, /mute, /unmute, /status, /help"""
import logging
import os
import re
from fastapi import APIRouter, Header, Request

from services import alerts, telegram, tg_users

logger = logging.getLogger(__name__)
router = APIRouter()

_METRICS = "price|ytm|dm|yidx|gspread"
_ALERT_RE = re.compile(
    rf"^/alert\s+(?P<isin>[A-Za-z0-9]{{12}})\s+(?P<side>buy|sell)\s+"
    rf"(?P<metric>{_METRICS})\s*(?P<op><=|>=)\s*(?P<thr>-?[\d.]+)"
    rf"(?:\s+vol\s+(?P<vol>[\d.eE+]+)\s*(?P<unit>rub|руб|шт|bonds)?)?\s*$",
    re.IGNORECASE)

_HELP = (
    "<b>Команды</b>\n"
    "/alert &lt;ISIN&gt; buy|sell price|ytm|dm|yidx|gspread &lt;=|&gt;= порог"
    " [vol N [rub]] — новый алерт\n"
    "  пример: <code>/alert RU000A10AU99 buy yidx >= 250 vol 1000000 rub</code>\n"
    "/alerts — список\n"
    "/del &lt;id&gt; — удалить\n"
    "/mute, /unmute — пауза доставки\n"
    "/status — состояние")


def _fmt_alert(a: dict) -> str:
    unit = {"rub": "₽", "bonds": "шт"}.get(a.get("volume_unit"), "")
    vol = (", vol ≥ " + f"{a['min_volume']:,.0f}".replace(",", " ") + f" {unit}"
           if a.get("min_volume") else "")
    status = {"active": "🟢", "fired": "⚡", "cancelled": "✖"}.get(a["status"], a["status"])
    return (f"{status} #{a['id']} {a['isin']} {a['side']} "
            f"{a['metric']} {a['op']} {a['threshold']:g}{vol}")


async def _handle_command(text: str, uid: int, chat_id: int, username: str) -> str:
    email = tg_users.email_for(uid)
    text = text.strip()

    if text.startswith("/start"):
        tg_users.upsert(uid, chat_id, username)
        return ("Флоатер-деск на связи. Алерты по стакану придут сюда "
                "картинкой.\n\n" + _HELP)

    if text.startswith("/help"):
        return _HELP

    if text.startswith("/alerts"):
        rows = alerts.list_for_user(email)
        if not rows:
            return "Алертов нет. Создать: см. /help"
        return "\n".join(_fmt_alert(a) for a in rows[:30])

    if text.startswith("/del"):
        m = re.match(r"^/del\s+(\d+)", text)
        if not m:
            return "Формат: /del <id>"
        ok = alerts.delete(email, int(m.group(1)))
        return "Удалён." if ok else "Не найден (чужой id?)."

    if text.startswith("/mute"):
        tg_users.set_muted(uid, True)
        return "🔇 Доставка на паузе. /unmute — вернуть."

    if text.startswith("/unmute"):
        tg_users.set_muted(uid, False)
        return "🔔 Доставка включена."

    if text.startswith("/status"):
        rows = alerts.list_for_user(email)
        active = sum(1 for a in rows if a["status"] == "active")
        fired = sum(1 for a in rows if a["status"] == "fired")
        u = tg_users.get(uid) or {}
        return (f"Алертов: {active} активных, {fired} сработавших.\n"
                f"Доставка: {'🔇 mute' if u.get('muted') else '🔔 on'}")

    m = _ALERT_RE.match(text)
    if m:
        unit = (m.group("unit") or "bonds").lower()
        unit = "rub" if unit in ("rub", "руб") else "bonds"
        try:
            a = alerts.create(
                email, isin=m.group("isin").upper(), side=m.group("side").lower(),
                metric=m.group("metric").lower(), op=m.group("op"),
                threshold=float(m.group("thr")),
                min_volume=float(m.group("vol") or 0), volume_unit=unit)
        except alerts.AlertError as e:
            return f"Ошибка: {e}"
        return "Создан:\n" + _fmt_alert(a)
    if text.startswith("/alert"):
        return "Не разобрал. Формат: см. /help"

    return "Не понял. /help"


@router.post("/webhook")
async def tg_webhook(request: Request,
                     x_telegram_bot_api_secret_token: str = Header(default="")):
    secret = os.getenv("TG_WEBHOOK_SECRET") or ""
    if not secret or x_telegram_bot_api_secret_token != secret:
        # 200 без обработки: 4xx заставит Telegram ретраить мусор бесконечно
        logger.warning("tg webhook: неверный secret token")
        return {"ok": True}
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    msg = update.get("message") or {}
    text = msg.get("text") or ""
    frm = msg.get("from") or {}
    chat = msg.get("chat") or {}
    uid, chat_id = frm.get("id"), chat.get("id")
    if not text or not uid or not chat_id or chat.get("type") != "private":
        return {"ok": True}
    if not tg_users.is_allowed(uid):
        logger.warning("tg webhook: отказ чужому tg_user_id=%s (@%s)",
                       uid, frm.get("username"))
        await telegram.send_message(chat_id, "Доступ закрыт.", parse_mode=None)
        return {"ok": True}
    try:
        reply = await _handle_command(text, uid, chat_id, frm.get("username") or "")
    except Exception as e:
        logger.warning("tg webhook handler error: %s", e)
        reply = "Внутренняя ошибка, см. логи."
    if reply:
        await telegram.send_message(chat_id, reply)
    return {"ok": True}
