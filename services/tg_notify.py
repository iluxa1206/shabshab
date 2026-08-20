"""Доставка в Telegram событий вкладки СИГНАЛЫ (фильтры стакана и крупные
сделки). Своей настройки у бота нет — адресат ищется по user_email через
привязку чата (services/tg_users.py).

События копятся в буфере и уходят пачкой раз в TG_SIGNAL_FLUSH_SEC — тик
скринера секундный, поштучная отправка выбила бы лимиты Bot API и превратила
чат в ленту. Ошибки доставки логируются и глотаются: веб-механика (лента
событий) уже отработала."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from services import telegram, tg_users

logger = logging.getLogger(__name__)

# (chat_id, filter_id) → {name, side, kind, matches} — буфер коалесценции сигналов
_pending: dict = {}
SIGNAL_FLUSH_SEC = float(os.getenv("TG_SIGNAL_FLUSH_SEC", "30"))
MAX_MATCHES = 8              # в одном сообщении; остальное сворачиваем в «ещё N»

_MSK = timezone(timedelta(hours=3))
# Ссылка на дашборд: имя выпуска в сообщении ведёт прямо в его карточку.
_SITE_URL = os.getenv("TG_SITE_URL", "https://assetallocator.ru/desk/app/")
_REASON = {"new": "заявка", "price": "цена", "spread": "спред", "money": "объём",
           "block": "крупная сделка"}


def _reason_delta(m: dict) -> str:
    """«спред +15 бп» / «объём +30 %» / «цена −0,6 п.п.» — насколько ушло с
    прошлого срабатывания. Единицы те же, что у порогов (services/signals)."""
    r = m.get("reason")
    if r == "new":
        return "заявка"
    if r == "money":
        prev, cur = m.get("prev_money_ok_rub"), m.get("money_ok_rub")
        if prev is None or cur is None or abs(prev) < 1:
            return "объём"
        pct = (cur - prev) / abs(prev) * 100.0
        return f"объём {pct:+.0f} %".replace("-", "−")
    if r == "spread":
        prev, cur = m.get("prev_val_bps"), m.get("val_bps")
        if prev is None or cur is None:
            return "спред"
        return f"спред {cur - prev:+.0f} бп".replace("-", "−")
    if r == "price":
        prev, cur = m.get("prev_price"), m.get("price")
        if prev is None or cur is None:
            return "цена"
        # запятая — только в самом числе: replace по всей строке съедал «п.п.»
        num = f"{cur - prev:+.2f}".replace(".", ",").replace("-", "−")
        return f"цена {num} п.п."
    return ""


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


def _num(v: Optional[float], digits: int = 2) -> str:
    """Число по-русски: запятая как разделитель, пробел между тысячами."""
    if v is None:
        return "—"
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def _fmt_money(v: Optional[float]) -> str:
    if not v:
        return ""
    return (f"{_num(v / 1e6, 1)} млн ₽" if v >= 1e6
            else f"{_num(v / 1e3, 0)} тыс ₽")


def _fmt_years(y: Optional[float]) -> str:
    """Срок до погашения: меньше года — в месяцах, иначе годы с десятой."""
    if y is None:
        return ""
    return f"{y * 12:.0f} мес" if y < 1 else f"{_num(y, 1)} г"


# Маркер строки: причина срабатывания у фильтра стакана, агрессор у сделки.
# Эмодзи здесь не украшение — в ленте чата глаз цепляется за него раньше, чем
# читает текст, и «новая заявка» отличается от «спред поехал» без чтения.
_REASON_ICON = {"new": "🆕", "spread": "📈", "money": "📦", "price": "💵"}
_SIDE_ICON = {"buy": "🟩", "sell": "🟥"}
_SIDE_WORD = {"buy": "покупка", "sell": "продажа"}


def _icon(m: dict, kind: str) -> str:
    if kind == "block":
        return _SIDE_ICON.get(m.get("side") or "", "⬜")
    r = m.get("reason") or ""
    if r == "spread":
        # спред разъехался в разные стороны — это разные новости
        prev, cur = m.get("prev_val_bps"), m.get("val_bps")
        if prev is not None and cur is not None and cur < prev:
            return "📉"
    return _REASON_ICON.get(r, "•")


def _issue_link(m: dict) -> str:
    """Имя выпуска ссылкой на его карточку: из чата один тап до стакана."""
    name = m.get("name") or m.get("isin") or "—"
    isin = m.get("isin")
    if not isin:
        return f"<b>{name}</b>"
    return f'<a href="{_SITE_URL}?isin={isin}&amp;ob=1"><b>{name}</b></a>'


def _fmt_match(m: dict, kind: str) -> str:
    """Две строки на бумагу: заголовок с главным числом и подстрочник с деталями.

    Одной строкой (как было) читалось как поток из восьми «·» — глазу не за что
    зацепиться. Здесь первая строка отвечает «что и насколько», вторая —
    «по какой цене, на сколько денег, какой это срок»."""
    head_bits = []
    sub_bits = []

    if kind == "block":
        money = _fmt_money(m.get("money_rub"))
        head_bits.append(f"<code>{money}</code>" if money else "")
        head_bits.append(_SIDE_WORD.get(m.get("side") or "", "без агрессора"))
        if m.get("val_bps") is not None:
            sub_bits.append(f"{m['val_bps']:.0f} бп")
        if m.get("price") is not None:
            sub_bits.append(f"{_num(m['price'])}%")
        if m.get("negotiated") is not None:
            sub_bits.append("адресная" if m["negotiated"] else "по стакану")
    else:
        if m.get("val_bps") is not None:
            head_bits.append(f"<code>{m['val_bps']:.0f} бп</code>")
        # причина ДЕЛЬТОЙ («спред +15 бп»): слово без величины не говорит,
        # стоит ли отрываться от текущего дела
        head_bits.append(_reason_delta(m) or _REASON.get(m.get("reason") or "", ""))
        if m.get("price") is not None:
            sub_bits.append(f"{_num(m['price'])}%")
        money = _fmt_money(m.get("money_rub"))
        if money:
            sub_bits.append(money)
        if m.get("single_px") is not None:
            sub_bits.append(f"одна заявка {_num(m['single_px'])}")
        elif m.get("levels"):
            sub_bits.append(f"набор {m['levels']} ур")

    years = _fmt_years(m.get("years"))
    if years:
        sub_bits.append(years)
    if m.get("rating"):
        sub_bits.append(str(m["rating"]))
    ts = (m.get("ts") or "")[11:16]
    if kind == "block" and ts:
        sub_bits.append(ts)

    head = f"{_icon(m, kind)} {_issue_link(m)} · " + " · ".join(b for b in head_bits if b)
    sub = " · ".join(b for b in sub_bits if b)
    return head + (f"\n<i>{sub}</i>" if sub else "")


def _signal_text(buf: dict) -> str:
    kind = "block" if buf.get("kind") == "block" else "book"
    ms = buf["matches"]
    n = len(ms)
    if kind == "block":
        word = "сделка" if n == 1 else ("сделки" if 2 <= n <= 4 else "сделок")
        head = f"💥 <b>Крупные сделки</b> · {n} {word}"
    else:
        side = {"ask": "оффер", "bid": "бид"}.get(buf.get("side") or "", "")
        word = "бумага" if n == 1 else ("бумаги" if 2 <= n <= 4 else "бумаг")
        head = f"📡 <b>{buf['name']}</b>" + (f" · {side}" if side else "") + f" · {n} {word}"
    body = "\n\n".join(_fmt_match(m, kind) for m in ms[:MAX_MATCHES])
    out = f"{head}\n\n{body}"
    if n > MAX_MATCHES:
        out += f"\n\n<i>…ещё {n - MAX_MATCHES}</i>"
    return out


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

async def tg_signal_worker() -> None:
    """Фон: раз в SIGNAL_FLUSH_SEC сливает буфер сигналов в чаты."""
    logger.info("tg_signal worker started (flush=%.0fs)", SIGNAL_FLUSH_SEC)
    while True:
        await asyncio.sleep(SIGNAL_FLUSH_SEC)
        try:
            await _flush_signals()
        except Exception as e:
            logger.warning("tg_notify flush error: %s", e)
