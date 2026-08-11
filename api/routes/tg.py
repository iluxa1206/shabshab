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
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel

from services import alerts, telegram, tg_users
from services.tg_webapp import InitDataError, validate_init_data

logger = logging.getLogger(__name__)
router = APIRouter()


def _webapp_url() -> Optional[str]:
    return os.getenv("TG_WEBAPP_URL") or None


def _webapp_markup() -> Optional[dict]:
    url = _webapp_url()
    if not url:
        return None
    return {"inline_keyboard": [[{"text": "⚙️ Настройка алертов",
                                  "web_app": {"url": url}}]]}

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
        await telegram.send_message(
            chat_id, "Флоатер-деск на связи. Алерты по стакану придут сюда "
            "картинкой.\n\n" + _HELP, reply_markup=_webapp_markup())
        return ""

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


# --- REST для Mini App (фаза 2). Auth — подписанный initData в заголовке
# Authorization: tma <initData>; per-request, серверной сессии нет. ---

async def require_tg(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Нет initData")
    try:
        user = validate_init_data(authorization[4:])
    except InitDataError as e:
        raise HTTPException(status_code=401, detail=f"initData: {e}")
    if not tg_users.is_allowed(user["tg_user_id"]):
        raise HTTPException(status_code=403, detail="Доступ закрыт")
    return user


class TgAlertCreate(BaseModel):
    isin: str
    side: str                    # buy | sell
    metric: str                  # price | ytm | dm | yidx | gspread
    op: str                      # '<=' | '>='
    threshold: float
    min_volume: float = 0.0
    volume_unit: str = "bonds"   # bonds | rub
    kind: str = "floater"
    note: Optional[str] = None


class TgAlertPatch(BaseModel):
    side: Optional[str] = None
    metric: Optional[str] = None
    op: Optional[str] = None
    threshold: Optional[float] = None
    min_volume: Optional[float] = None
    volume_unit: Optional[str] = None
    note: Optional[str] = None


@router.get("/me", tags=["TG"])
async def tg_me(user: dict = Depends(require_tg)):
    row = tg_users.get(user["tg_user_id"]) or {}
    return {"tg_user_id": user["tg_user_id"], "username": user.get("username"),
            "muted": bool(row.get("muted")), "registered": bool(row)}


@router.get("/alerts", tags=["TG"])
async def tg_list_alerts(user: dict = Depends(require_tg)):
    rows = alerts.list_for_user(tg_users.email_for(user["tg_user_id"]))
    try:
        from services import instruments_registry
        labels = instruments_registry.labels_map([a["isin"] for a in rows])
        for a in rows:
            a["name"] = (labels.get(a["isin"]) or {}).get("name")
    except Exception:
        pass
    return {"alerts": rows}


@router.post("/alerts", tags=["TG"])
async def tg_create_alert(body: TgAlertCreate, user: dict = Depends(require_tg)):
    try:
        return alerts.create(
            tg_users.email_for(user["tg_user_id"]), isin=body.isin, side=body.side,
            metric=body.metric, op=body.op, threshold=body.threshold,
            min_volume=body.min_volume, volume_unit=body.volume_unit,
            kind=body.kind, note=body.note)
    except alerts.AlertError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/alerts/{aid}", tags=["TG"])
async def tg_patch_alert(body: TgAlertPatch, aid: int = Path(...),
                         user: dict = Depends(require_tg)):
    try:
        a = alerts.update(tg_users.email_for(user["tg_user_id"]), aid,
                          **body.model_dump(exclude_none=True))
    except alerts.AlertError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if a is None:
        raise HTTPException(status_code=404, detail="Алерт не найден")
    return a


@router.delete("/alerts/{aid}", tags=["TG"])
async def tg_delete_alert(aid: int = Path(...), user: dict = Depends(require_tg)):
    if not alerts.delete(tg_users.email_for(user["tg_user_id"]), aid):
        raise HTTPException(status_code=404, detail="Алерт не найден")
    return {"ok": True}


class MuteBody(BaseModel):
    muted: bool


@router.post("/mute", tags=["TG"])
async def tg_mute(body: MuteBody, user: dict = Depends(require_tg)):
    tg_users.set_muted(user["tg_user_id"], body.muted)
    return {"muted": body.muted}


@router.get("/search", tags=["TG"])
async def tg_search(q: str = "", user: dict = Depends(require_tg)):
    from services import instruments_registry
    return {"results": instruments_registry.search(q, limit=10)}


# --- скринер-фильтры (фаза 3) ---

class FilterParams(BaseModel):
    op: str = ">="
    threshold: float = 250.0
    src: str = "ask"                    # ask | last
    base: Optional[str] = None          # KEYRATE | RUONIA | None
    rating_min: Optional[str] = None
    max_years: Optional[float] = None
    min_depth_rub: Optional[float] = None


class FilterCreate(BaseModel):
    name: str
    params: FilterParams = FilterParams()
    cooldown_min: int = 240


class FilterPatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    params: Optional[FilterParams] = None
    cooldown_min: Optional[int] = None


@router.get("/filters", tags=["TG"])
async def tg_list_filters(user: dict = Depends(require_tg)):
    from services import tg_screener
    return {"filters": tg_screener.list_for_user(user["tg_user_id"])}


@router.post("/filters", tags=["TG"])
async def tg_create_filter(body: FilterCreate, user: dict = Depends(require_tg)):
    from services import tg_screener
    try:
        return tg_screener.create(user["tg_user_id"], body.name,
                                  body.params.model_dump(), body.cooldown_min)
    except tg_screener.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/filters/{fid}", tags=["TG"])
async def tg_patch_filter(body: FilterPatch, fid: int = Path(...),
                          user: dict = Depends(require_tg)):
    from services import tg_screener
    try:
        f = tg_screener.update(
            user["tg_user_id"], fid, name=body.name, enabled=body.enabled,
            params=body.params.model_dump() if body.params else None,
            cooldown_min=body.cooldown_min)
    except tg_screener.FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if f is None:
        raise HTTPException(status_code=404, detail="Фильтр не найден")
    return f


@router.delete("/filters/{fid}", tags=["TG"])
async def tg_delete_filter(fid: int = Path(...), user: dict = Depends(require_tg)):
    from services import tg_screener
    if not tg_screener.delete(user["tg_user_id"], fid):
        raise HTTPException(status_code=404, detail="Фильтр не найден")
    return {"ok": True}
