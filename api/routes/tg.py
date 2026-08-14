"""Телеграм-бот: вебхук + управление привязкой чатов (только админ).

Настройки у бота своей нет — алерты и сигналы заводятся на сайте и дублируются
в привязанный чат (services/tg_notify.py). Вебхук вне require_user: защита —
секрет-заголовок X-Telegram-Bot-Api-Secret-Token (env TG_WEBHOOK_SECRET) плюс
статус привязки в tg_users. Команды бота — только чтение и пауза доставки:
  /start — заявка на доступ, /alerts — список, /signals — последние события,
  /mute, /unmute, /status, /help
"""
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel

from api.routes.auth import require_admin
from services import alerts, auth_users, signals, telegram, tg_users

logger = logging.getLogger(__name__)
router = APIRouter()

_SITE_URL = os.getenv("TG_SITE_URL", "https://assetallocator.ru/desk/")

_HELP = (
    "<b>Что умеет бот</b>\n"
    "Алерты по стакану и сигналы рынка настраиваются на сайте — сюда приходят "
    "их копии.\n\n"
    "/alerts — мои алерты\n"
    "/signals — последние сигналы\n"
    "/mute, /unmute — пауза доставки\n"
    "/status — состояние привязки\n"
    f'<a href="{_SITE_URL}">Открыть дашборд</a>')


def _fmt_alert(a: dict) -> str:
    unit = {"rub": "₽", "bonds": "шт"}.get(a.get("volume_unit"), "")
    vol = (", vol ≥ " + f"{a['min_volume']:,.0f}".replace(",", " ") + f" {unit}"
           if a.get("min_volume") else "")
    status = {"active": "🟢", "fired": "⚡", "cancelled": "✖"}.get(a["status"], a["status"])
    return (f"{status} #{a['id']} {a['isin']} {a['side']} "
            f"{a['metric']} {a['op']} {a['threshold']:g}{vol}")


def _fmt_event(e: dict) -> str:
    name = e.get("name") or e.get("isin")
    bits = []
    if e.get("val_bps") is not None:
        bits.append(f"{e['val_bps']:.0f} бп")
    if e.get("price") is not None:
        bits.append(f"{e['price']:.2f}")
    if e.get("money_rub"):
        v = e["money_rub"]
        bits.append(f"{v / 1e6:.1f} млн ₽" if v >= 1e6 else f"{v / 1e3:.0f} тыс ₽")
    ts = (e.get("fired_at") or "")[11:16]
    return f"• {ts} <b>{name}</b> — " + ", ".join(bits)


async def _handle_command(text: str, uid: int, chat_id: int, username: str) -> str:
    email = tg_users.email_for(uid)
    text = text.strip()

    if text.startswith("/start"):
        return ("Флоатер-деск на связи. Алерты и сигналы, заведённые на сайте, "
                "будут дублироваться сюда.\n\n" + _HELP)

    if text.startswith("/help"):
        return _HELP

    if text.startswith("/alerts"):
        rows = alerts.list_for_user(email)
        if not rows:
            return "Алертов нет. Заводятся на сайте, в стакане выпуска."
        return "\n".join(_fmt_alert(a) for a in rows[:30])

    if text.startswith("/signals"):
        rows = signals.events_for_user(email, limit=15)
        if not rows:
            return "Сигналов пока не было. Фильтры — на сайте, вкладка СИГНАЛЫ."
        return "\n".join(_fmt_event(e) for e in rows)

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
        nfilters = len(signals.list_for_user(email))
        return (f"Аккаунт: {email}\n"
                f"Алертов: {active} активных, {fired} сработавших.\n"
                f"Фильтров сигналов: {nfilters}.\n"
                f"Доставка: {'🔇 mute' if u.get('muted') else '🔔 on'}")

    return "Не понял. /help"


async def process_update(update: dict) -> None:
    """Разбор одного апдейта Bot API. Общий для вебхука и long polling
    (services/tg_poll.py): на прод-VPS вебхук недоступен — Telegram не может
    открыть к нам соединение, — поэтому боевой путь именно поллер."""
    msg = update.get("message") or {}
    text = msg.get("text") or ""
    frm = msg.get("from") or {}
    chat = msg.get("chat") or {}
    uid, chat_id = frm.get("id"), chat.get("id")
    username = frm.get("username") or ""
    if not text or not uid or not chat_id or chat.get("type") != "private":
        return

    if not tg_users.is_allowed(uid):
        # Заявку заводим на любое сообщение: юзер мог начать не с /start.
        # Одобряет админ на сайте, до этого бот молчит по делу.
        try:
            row = tg_users.request_access(uid, chat_id, username)
        except Exception as e:
            logger.warning("tg: заявка %s не сохранена: %s", uid, e)
            row = None
        logger.info("tg: заявка от tg_user_id=%s (@%s) status=%s",
                    uid, username, (row or {}).get("status"))
        if (row or {}).get("status") == "rejected":
            await telegram.send_message(chat_id, "Доступ закрыт.", parse_mode=None)
        else:
            await telegram.send_message(
                chat_id,
                "Заявка на доступ принята. Админ привяжет этот чат к вашему "
                "аккаунту на сайте — после этого сюда пойдут алерты и сигналы."
                + (f"\nВаш ник: @{username}" if username else
                   f"\nВаш ID: {uid} (ника нет — передайте админу его)"),
                parse_mode=None)
        return

    # известный чат: держим chat_id/username свежими (юзер мог сменить ник)
    try:
        tg_users.request_access(uid, chat_id, username)
    except Exception as e:
        logger.warning("tg: upsert %s: %s", uid, e)
    try:
        reply = await _handle_command(text, uid, chat_id, username)
    except Exception as e:
        logger.warning("tg handler error: %s", e)
        reply = "Внутренняя ошибка, см. логи."
    if reply:
        await telegram.send_message(chat_id, reply)


@router.post("/webhook")
async def tg_webhook(request: Request,
                     x_telegram_bot_api_secret_token: str = Header(default="")):
    """Оставлен для сети, где Telegram до нас достучится. На текущем VPS не
    работает (Connection timed out со стороны Telegram) — там включён поллер."""
    secret = os.getenv("TG_WEBHOOK_SECRET") or ""
    if not secret or x_telegram_bot_api_secret_token != secret:
        # 200 без обработки: 4xx заставит Telegram ретраить мусор бесконечно
        logger.warning("tg webhook: неверный secret token")
        return {"ok": True}
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    await process_update(update)
    return {"ok": True}


# --- привязка чатов к аккаунтам (админка сайта) ---

class ApproveBody(BaseModel):
    email: str


def _link_row(r: dict) -> dict:
    return {"tg_user_id": r["tg_user_id"], "username": r.get("username"),
            "email": r.get("email"), "status": r.get("status"),
            "muted": bool(r.get("muted")), "created_at": r.get("created_at"),
            "approved_at": r.get("approved_at"), "approved_by": r.get("approved_by")}


@router.get("/links", tags=["TG"])
async def tg_list_links(_admin: dict = Depends(require_admin)):
    return {"links": [_link_row(r) for r in tg_users.list_all()],
            "enabled": telegram.enabled()}


@router.post("/links/{uid}/approve", tags=["TG"])
async def tg_approve_link(body: ApproveBody, uid: int = Path(...),
                          admin: dict = Depends(require_admin)):
    email = (body.email or "").strip().lower()
    if not auth_users.get_user(email):
        raise HTTPException(status_code=400, detail=f"нет пользователя {email}")
    try:
        row = tg_users.approve(uid, email, admin["email"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    logger.info("tg link approved: %s → %s by %s", uid, email, admin["email"])
    try:
        await telegram.send_message(
            row["chat_id"],
            f"Доступ открыт: чат привязан к аккаунту {email}.\n"
            "Алерты и сигналы с сайта пойдут сюда. /help — команды.")
    except Exception as e:
        logger.warning("tg approve notify error: %s", e)
    return _link_row(row)


@router.post("/links/{uid}/revoke", tags=["TG"])
async def tg_revoke_link(uid: int = Path(...), admin: dict = Depends(require_admin)):
    row = tg_users.revoke(uid)
    if row is None:
        raise HTTPException(status_code=404, detail="Привязка не найдена")
    logger.info("tg link revoked: %s by %s", uid, admin["email"])
    return _link_row(row)


@router.delete("/links/{uid}", tags=["TG"])
async def tg_delete_link(uid: int = Path(...), admin: dict = Depends(require_admin)):
    if not tg_users.delete(uid):
        raise HTTPException(status_code=404, detail="Привязка не найдена")
    logger.info("tg link deleted: %s by %s", uid, admin["email"])
    return {"ok": True}
