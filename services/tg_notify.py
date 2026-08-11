"""Доставка сработавших алертов в Telegram: очередь + фоновый consumer
(api.main запускает tg_notify_worker). Монитор алертов кладёт событие
неблокирующе — рендер PNG и HTTP к Bot API не тормозят цикл мониторинга.

Доставляются только алерты пользователей бота (user_email 'tg:<id>');
веб-алерты живут как раньше. Ошибки доставки логируются и глотаются —
алерт-механика (mark_fired) уже отработала."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from services import telegram, tg_users

logger = logging.getLogger(__name__)

_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=200)

_MSK = timezone(timedelta(hours=3))
_METRIC_LABEL = {"price": "цена", "ytm": "YTM", "dm": "DM",
                 "yidx": "R-spread", "gspread": "G-спред"}
_METRIC_UNIT = {"price": "", "ytm": "%", "dm": " бп", "yidx": " бп", "gspread": " бп"}


def enqueue(alert: dict, bids: List[dict], asks: List[dict],
            face: Optional[float], hit: dict) -> None:
    """Из alerts_monitor, сразу после mark_fired. Никогда не бросает."""
    if not telegram.enabled():
        return
    if not (alert.get("user_email") or "").startswith(tg_users.EMAIL_PREFIX):
        return
    try:
        _queue.put_nowait({"alert": alert, "bids": bids, "asks": asks,
                           "face": face, "hit": hit})
    except asyncio.QueueFull:
        logger.warning("tg_notify queue full, drop alert id=%s", alert.get("id"))


def _short_name(isin: str) -> Optional[str]:
    try:
        from services import instruments_registry
        row = instruments_registry.get(isin)
        return (row or {}).get("short_name")
    except Exception:
        return None


def _caption(alert: dict, face: Optional[float], hit: dict, name: Optional[str]) -> str:
    isin = alert["isin"]
    metric = _METRIC_LABEL.get(alert["metric"], alert["metric"])
    unit = _METRIC_UNIT.get(alert["metric"], "")
    side = "покупка" if alert["side"] == "buy" else "продажа"
    price, vol = hit.get("price"), hit.get("volume")
    lines = [f"⚡ <b>{name or isin}</b> — алерт #{alert['id']}",
             f"{side}: {metric} {alert['op']} {alert['threshold']:g}{unit} — сработал",
             f"цена {price:.2f}" + (f", объём {vol:,.0f} шт".replace(",", " ") if vol else "")]
    if face and price and vol:
        rub = vol * face * price / 100.0
        lines[-1] += f" (~{rub / 1e6:.2f} млн ₽)"
    if alert.get("note"):
        lines.append(f"<i>{alert['note']}</i>")
    return "\n".join(lines)


async def _deliver(ev: dict) -> None:
    alert, hit = ev["alert"], ev["hit"]
    user = tg_users.chat_for_email(alert["user_email"])
    if not user:
        logger.warning("tg_notify: нет tg_users для %s", alert["user_email"])
        return
    if user.get("muted"):
        return
    name = _short_name(alert["isin"])
    caption = _caption(alert, ev["face"], hit, name)
    png = None
    try:
        from services.tg_render import render_orderbook
        png = await asyncio.to_thread(
            render_orderbook,
            isin=alert["isin"], name=name, kind=alert.get("kind") or "floater",
            bids=ev["bids"], asks=ev["asks"],
            hit_price=hit.get("price"),
            hit_side="sell" if alert["side"] == "buy" else "buy",
            title="АЛЕРТ", ts=datetime.now(_MSK))
    except Exception as e:
        logger.warning("tg render error (alert %s): %s", alert.get("id"), e)
    if png:
        ok = await telegram.send_photo(user["chat_id"], png, caption)
    else:
        ok = await telegram.send_message(user["chat_id"], caption)
    if ok is None:
        logger.warning("tg_notify: доставка алерта %s не удалась", alert.get("id"))


async def tg_notify_worker() -> None:
    """Фон: разбирает очередь доставки. Троттлинг лимитов Bot API внутри
    telegram.call (ретраи на 429)."""
    logger.info("tg_notify worker started (enabled=%s)", telegram.enabled())
    while True:
        ev = await _queue.get()
        try:
            await _deliver(ev)
        except Exception as e:
            logger.warning("tg_notify deliver error: %s", e)
        finally:
            _queue.task_done()
