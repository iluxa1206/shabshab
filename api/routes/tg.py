"""Телеграм-бот: вебхук + управление привязкой чатов (только админ).

Настройки у бота своей нет — сигналы заводятся на сайте и дублируются в
привязанный чат (services/tg_notify.py). Вебхук вне require_user: защита —
секрет-заголовок X-Telegram-Bot-Api-Secret-Token (env TG_WEBHOOK_SECRET) плюс
статус привязки в tg_users. Команды бота — только чтение и пауза доставки:
  /start — заявка на доступ, /signals — последние события,
  /mute, /unmute, /status, /custom, /help
"""
import html
import logging
import os

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel

from api.routes.auth import require_admin
from services import auth_users, signals, telegram, tg_targets, tg_users

logger = logging.getLogger(__name__)
router = APIRouter()

_SITE_URL = os.getenv("TG_SITE_URL", "https://assetallocator.ru/desk/")

_HELP = (
    "<b>Что умеет бот</b>\n"
    "Сигналы рынка настраиваются на сайте — сюда приходят их копии.\n\n"
    "/signals — последние сигналы\n"
    "/custom — свои эмодзи для маркеров\n"
    "/chats — каналы для доставки (перешлите сюда пост из канала)\n"
    "/mute, /unmute — пауза доставки\n"
    "/status — состояние привязки\n"
    f'<a href="{_SITE_URL}">Открыть дашборд</a>')


def _fmt_event(e: dict) -> str:
    name = e.get("name") or e.get("isin")
    bits = []
    if e.get("val_bps") is not None:
        bits.append(f"R-spread {e['val_bps']:.0f} бп")
    if e.get("price") is not None:
        bits.append(f"{e['price']:.2f}")
    if e.get("money_rub"):
        v = e["money_rub"]
        bits.append(f"{v / 1e6:.1f} млн ₽" if v >= 1e6 else f"{v / 1e3:.0f} тыс ₽")
    ts = (e.get("fired_at") or "")[11:16]
    return f"• {ts} <b>{name}</b> — " + ", ".join(bits)


def _targets_text(email: str) -> str:
    """Список каналов аккаунта + как добавить новый."""
    rows = tg_targets.list_for_user(email)
    body = ("\n".join(f"<code>{t['id']}</code>  {html.escape(t['title'])}"
                      for t in rows)
            if rows else "Пока ни одного — сигналы идут сюда, в личку.")
    return ("<b>Каналы доставки</b>\n" + body +
            "\n\nДобавить: добавьте бота в канал администратором и перешлите "
            "сюда любой пост оттуда.\nУбрать: <code>/chats del 1</code>\n"
            "Какой фильтр куда слать — выбирается на сайте, вкладка СИГНАЛЫ.")


def _forward_chat(msg: dict) -> Optional[dict]:
    """Исходный чат пересланного сообщения → {id, title, kind}.

    Bot API 7.0 заменил forward_from_chat на forward_origin; поддерживаем оба —
    у пользователя может быть клиент любой свежести, а поле приходит от него."""
    src = msg.get("forward_from_chat")
    if not src:
        origin = msg.get("forward_origin") or {}
        src = origin.get("chat") if origin.get("type") == "channel" else None
    if not src or not src.get("id"):
        return None
    kind = src.get("type") or "channel"
    return {"id": int(src["id"]),
            "title": src.get("title") or src.get("username") or str(src["id"]),
            "kind": "group" if kind in ("group", "supergroup") else kind}


async def _bind_forwarded(msg: dict, email: str) -> Optional[str]:
    """Переслали пост из канала → регистрируем канал как адресата.

    Сразу пишем в него: право писать есть только у администратора, и узнать
    об этом лучше здесь, чем на первом пропавшем сигнале."""
    src = _forward_chat(msg)
    if not src:
        return None
    probe = await telegram.send_message(
        src["id"], "Канал привязан к деску: сюда будут приходить сигналы "
                   "выбранных фильтров.")
    if probe is None:
        why = (telegram.last_error.get("description") or "").lower()
        hint = ("бот должен быть АДМИНИСТРАТОРОМ канала"
                if "not enough rights" in why or "forbidden" in why or "chat not found" in why
                else "проверьте права бота в канале")
        return f"Не смог написать в «{html.escape(src['title'])}»: {hint}."
    try:
        t = tg_targets.add(email, src["id"], src["title"], src["kind"])
    except ValueError as e:
        return str(e)
    return (f"Канал «{html.escape(t['title'])}» добавлен (№{t['id']}).\n"
            "Выберите его в настройках фильтра на сайте — вкладка СИГНАЛЫ.")


def _icons_text(icons: dict) -> str:
    """Текущий набор маркеров + как его менять."""
    rows = "\n".join(
        f"{icons.get(slot, dflt)}  {title} — <code>{slot}</code>"
        for slot, (title, dflt) in tg_users.ICON_SLOTS.items())
    return ("<b>Маркеры строк</b>\n" + rows +
            "\n\nСменить: <code>/custom ask 🟠</code> "
            "(слот можно и по-русски: <code>/custom оффер 🟠</code>)\n"
            "Вернуть один: <code>/custom ask -</code>\n"
            "Вернуть все: <code>/custom reset</code>")


# Маркер — это ЭМОДЗИ, а не подпись: он стоит первым символом строки, и текст
# там ломает вёрстку (и разбор HTML, если в нём окажется «<»). Пропускаем
# короткие строки без букв, цифр и служебных символов — символьные значки
# (▲, ●) тоже годятся, они ничего не ломают.
_ICON_MAX_LEN = 8
_ICON_BAD = set("<>&\"'/\\")


def _valid_icon(v: str) -> bool:
    if not v or len(v) > _ICON_MAX_LEN:
        return False
    if any(ch in _ICON_BAD for ch in v):
        return False
    return not any(ch.isalnum() or ch.isspace() for ch in v)


async def _handle_command(text: str, uid: int, chat_id: int, username: str) -> str:
    email = tg_users.email_for(uid)
    text = text.strip()

    if text.startswith("/start"):
        return ("Флоатер-деск на связи. Сигналы, заведённые на сайте, "
                "будут дублироваться сюда.\n\n" + _HELP)

    if text.startswith("/help"):
        return _HELP

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

    if text.startswith("/custom"):
        parts = text.split()[1:]
        row = tg_users.get(uid)
        if not parts:
            return _icons_text(tg_users.icons(row))
        if parts[0].lower() in ("reset", "сброс"):
            return "Маркеры вернулись к стандартным.\n\n" + _icons_text(
                tg_users.reset_icons(uid))
        if len(parts) < 2:
            return ("Нужно два слова: слот и эмодзи.\n\n"
                    + _icons_text(tg_users.icons(row)))
        slot = tg_users.slot_key(parts[0])
        if not slot:
            names = ", ".join(f"<code>{k}</code>" for k in tg_users.ICON_SLOTS)
            return f"Слота «{html.escape(parts[0])}» нет. Есть: {names}."
        value = parts[1]
        if value in ("-", "—"):
            return "Вернул стандартный.\n\n" + _icons_text(
                tg_users.set_icon(uid, slot, None))
        if not _valid_icon(value):
            return ("Маркером может быть только эмодзи или значок — "
                    "без букв, цифр и пробелов.")
        return "Готово.\n\n" + _icons_text(tg_users.set_icon(uid, slot, value))

    if text.startswith("/chats"):
        parts = text.split()[1:]
        if parts and parts[0].lower() in ("del", "remove", "удалить"):
            if len(parts) < 2 or not parts[1].isdigit():
                return "Нужен номер: <code>/chats del 3</code>"
            ok = tg_targets.remove(email, int(parts[1]))
            return ("Адресат снят — фильтры на нём вернутся в личку."
                    if ok else "Такого адресата нет.")
        return _targets_text(email)

    if text.startswith("/status"):
        u = tg_users.get(uid) or {}
        fs = signals.list_for_user(email)
        on = sum(1 for f in fs if f.get("enabled"))
        return (f"Аккаунт: {email}\n"
                f"Фильтров сигналов: {len(fs)} ({on} включённых).\n"
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
    forwarded = bool(msg.get("forward_from_chat") or msg.get("forward_origin"))
    if not uid or not chat_id or chat.get("type") != "private":
        return
    if not text and not forwarded:      # пересылка бывает и без текста (фото, опрос)
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
    email = tg_users.email_for(uid)
    if email:
        try:
            bound = await _bind_forwarded(msg, email)
        except Exception as e:
            logger.warning("tg: привязка канала: %s", e)
            bound = "Не удалось привязать канал, см. логи."
        if bound:
            await telegram.send_message(chat_id, bound)
            return
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
