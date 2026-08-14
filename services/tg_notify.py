"""Доставка в Telegram того, что настроено на сайте: алерты по стакану и
события вкладки СИГНАЛЫ. Своей настройки у бота нет — адресат ищется по
user_email через привязку чата (services/tg_users.py).

Алерты уходят сразу (очередь + consumer: рендер PNG и HTTP к Bot API не тормозят
цикл мониторинга). Сигналы копятся в буфере и уходят пачкой раз в
TG_SIGNAL_FLUSH_SEC — тик скринера секундный, поштучная отправка выбила бы
лимиты Bot API и превратила чат в ленту. Ошибки доставки логируются и глотаются:
веб-механика (mark_fired, лента событий) уже отработала."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from services import telegram, tg_users

logger = logging.getLogger(__name__)

_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=200)

# (chat_id, filter_id) → {name, side, kind, matches} — буфер коалесценции сигналов
_pending: dict = {}
SIGNAL_FLUSH_SEC = float(os.getenv("TG_SIGNAL_FLUSH_SEC", "30"))
MAX_MATCHES = 8              # в одном сообщении; остальное сворачиваем в «ещё N»

_MSK = timezone(timedelta(hours=3))
_METRIC_LABEL = {"price": "цена", "ytm": "YTM", "dm": "DM",
                 "yidx": "R-spread", "gspread": "G-спред"}
_METRIC_UNIT = {"price": "", "ytm": "%", "dm": " бп", "yidx": " бп", "gspread": " бп"}
_REASON = {"new": "новая", "price": "цена", "spread": "спред", "money": "объём",
           "block": "крупная сделка"}


# --- алерты по стакану ---

def enqueue(alert: dict, bids: List[dict], asks: List[dict],
            face: Optional[float], hit: dict) -> None:
    """Из alerts_monitor, сразу после mark_fired. Никогда не бросает."""
    if not telegram.enabled():
        return
    try:
        if not tg_users.has_chats(alert.get("user_email") or ""):
            return
        _queue.put_nowait({"kind": "alert", "alert": alert, "bids": bids,
                           "asks": asks, "face": face, "hit": hit})
    except asyncio.QueueFull:
        logger.warning("tg_notify queue full, drop alert id=%s", alert.get("id"))
    except Exception as e:                       # БД недоступна — не роняем монитор
        logger.warning("tg_notify enqueue error: %s", e)


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


async def _deliver_alert(ev: dict) -> None:
    alert, hit = ev["alert"], ev["hit"]
    chats = tg_users.chats_for_email(alert.get("user_email") or "")
    if not chats:
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
    for u in chats:
        if png:
            ok = await telegram.send_photo(u["chat_id"], png, caption)
        else:
            ok = await telegram.send_message(u["chat_id"], caption)
        if ok is None:
            logger.warning("tg_notify: доставка алерта %s в чат %s не удалась",
                           alert.get("id"), u["chat_id"])


# --- события вкладки СИГНАЛЫ ---

def enqueue_signal(user_email: str, filter_id: int, filter_name: str,
                   side: Optional[str], matches: List[dict],
                   kind: str = "book") -> None:
    """Из signals.run_cycle / block_trades.notify_blocks — рядом с WS-пушем.
    Складывает в буфер: отправка пачкой из flush-воркера. Никогда не бросает."""
    if not telegram.enabled() or not matches:
        return
    try:
        chats = tg_users.chats_for_email(user_email)
        if not chats:
            return
        for u in chats:
            buf = _pending.setdefault(
                (u["chat_id"], filter_id),
                {"name": filter_name, "side": side, "kind": kind, "matches": []})
            buf["name"], buf["side"], buf["kind"] = filter_name, side, kind
            # хвост длинной серии интереснее её начала: держим последние
            buf["matches"] = (buf["matches"] + list(matches))[-40:]
    except Exception as e:
        logger.warning("tg_notify enqueue_signal error: %s", e)


def _fmt_money(v: Optional[float]) -> str:
    if not v:
        return ""
    return f"{v / 1e6:.1f} млн ₽" if v >= 1e6 else f"{v / 1e3:.0f} тыс ₽"


def _fmt_years(y: Optional[float]) -> str:
    """Срок до погашения: меньше года — в месяцах, иначе годы с десятой."""
    if y is None:
        return ""
    return f"{y * 12:.0f} мес" if y < 1 else f"{y:.1f} г"


def _fmt_match(m: dict, kind: str) -> str:
    """Строка одной бумаги/сделки. Подписываем всё, что иначе читается двояко:
    базу спреда (Y-IDX), сторону, режим торгов и срок до погашения — те же
    слова, что в ленте на сайте (frontend-react/src/signalFormat.js)."""
    name = m.get("name") or m.get("isin")
    bits = []
    if kind == "block":
        # сторона сделки — агрессор; у адресной его нет вовсе
        bits.append({"buy": "покупка", "sell": "продажа"}.get(
            m.get("side") or "", "без агрессора"))
    if m.get("val_bps") is not None:
        bits.append(f"Y-IDX {m['val_bps']:.0f} бп")
    if m.get("price") is not None:
        bits.append(f"{m['price']:.2f}")
    money = _fmt_money(m.get("money_rub"))
    if money:
        bits.append(money)
    if kind == "block":
        if m.get("negotiated") is not None:
            bits.append("адресная (РПС)" if m["negotiated"] else "по стакану")
    elif m.get("single_px") is not None:
        bits.append(f"одна заявка {m['single_px']:.2f}")
    elif m.get("levels"):
        bits.append(f"набор {m['levels']} ур")
    years = _fmt_years(m.get("years"))
    if years:
        bits.append(years)
    reason = _REASON.get(m.get("reason") or "", "")
    if reason and kind != "block":
        bits.append(reason)
    return f"• <b>{name}</b> — " + " · ".join(bits)


def _signal_text(buf: dict) -> str:
    side = {"ask": "оффер", "bid": "бид"}.get(buf.get("side") or "", "")
    kind = buf.get("kind") or "book"
    head = "💥 <b>Крупная сделка</b>" if kind == "block" else "📡 <b>Сигнал</b>"
    lines = [f"{head} — {buf['name']}" + (f" ({side})" if side else "")]
    ms = buf["matches"]
    lines += [_fmt_match(m, kind) for m in ms[:MAX_MATCHES]]
    if len(ms) > MAX_MATCHES:
        lines.append(f"…ещё {len(ms) - MAX_MATCHES}")
    return "\n".join(lines)


async def _flush_signals() -> None:
    if not _pending:
        return
    batch = list(_pending.items())
    _pending.clear()
    for (chat_id, _fid), buf in batch:
        try:
            await telegram.send_message(chat_id, _signal_text(buf))
        except Exception as e:
            logger.warning("tg_notify signal send error (chat %s): %s", chat_id, e)


# --- воркеры ---

async def tg_notify_worker() -> None:
    """Фон: разбирает очередь доставки алертов. Троттлинг лимитов Bot API
    внутри telegram.call (ретраи на 429)."""
    logger.info("tg_notify worker started (enabled=%s)", telegram.enabled())
    while True:
        ev = await _queue.get()
        try:
            await _deliver_alert(ev)
        except Exception as e:
            logger.warning("tg_notify deliver error: %s", e)
        finally:
            _queue.task_done()


async def tg_signal_worker() -> None:
    """Фон: раз в SIGNAL_FLUSH_SEC сливает буфер сигналов в чаты."""
    logger.info("tg_signal worker started (flush=%.0fs)", SIGNAL_FLUSH_SEC)
    while True:
        await asyncio.sleep(SIGNAL_FLUSH_SEC)
        try:
            await _flush_signals()
        except Exception as e:
            logger.warning("tg_notify flush error: %s", e)
