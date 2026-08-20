"""Доставка в Telegram событий вкладки СИГНАЛЫ (фильтры стакана и крупные
сделки). Своей настройки у бота нет — адресат ищется по user_email через
привязку чата (services/tg_users.py).

События копятся в буфере и уходят пачкой раз в TG_SIGNAL_FLUSH_SEC — тик
скринера секундный, поштучная отправка выбила бы лимиты Bot API и превратила
чат в ленту. Ошибки доставки логируются и глотаются: веб-механика (лента
событий) уже отработала."""
import asyncio
import html
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

def _group(matches: List[dict], kind: str):
    """Как бить события по сообщениям.

    ЗАЯВКИ — по одному сообщению на выпуск: к каждой прикладывается снимок
    стакана, и склеенные в пачку они превращались бы в простыню из лестниц.
    Заодно повторы по одной бумаге внутри такта схлопываются в последнее
    состояние. СДЕЛКИ остаются пачкой: их читают потоком, стакан к ним не
    прикладывается."""
    if kind != "book":
        return [(None, matches)]
    by_isin: dict = {}
    for m in matches:
        by_isin.setdefault(m.get("isin"), []).append(m)
    return list(by_isin.items())


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
            for group, ms in _group(matches, kind):
                buf = _pending.setdefault(
                    (u["chat_id"], filter_id, group),
                    {"name": filter_name, "side": side, "kind": kind, "matches": []})
                buf["name"], buf["side"], buf["kind"] = filter_name, side, kind
                # хвост длинной серии интереснее её начала: держим последние
                buf["matches"] = (buf["matches"] + list(ms))[-40:]
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


# Маркер строки — первое, что видит глаз, поэтому он про СТОРОНУ, а не про
# причину: у заявки это сторона стакана (оффер красный, бид зелёный — торговая
# конвенция), у сделки направление агрессора, а у адресной агрессора нет вовсе,
# и рукопожатие говорит это без слов.
_BOOK_ICON = {"ask": "🔴", "bid": "🟢"}
_TRADE_ICON = {"buy": "👍", "sell": "👎"}
_NDM_ICON = "🤝"


def _short_money(v: Optional[float]) -> str:
    """Деньги коротко: «1м ₽», «26,1м ₽», «300к ₽». В строке сигнала важен
    порядок суммы, а не копейки — длинное «26,1 млн ₽» съедает место у цифр,
    ради которых сообщение и открывают."""
    if not v:
        return ""
    unit, scaled = ("м", v / 1e6) if abs(v) >= 1e6 else ("к", v / 1e3)
    txt = _num(scaled, 1)
    if txt.endswith(",0"):          # «1,0м» читается хуже, чем «1м»
        txt = txt[:-2]
    return f"{txt}{unit} ₽"


def _hhmm(m: dict) -> str:
    """Время сделки, МСК. В живом пуше приходит ts самой сделки; в строке из
    ленты его нет (в таблице событий хранится только fired_at), поэтому берём
    момент срабатывания и переводим из UTC."""
    ts = (m.get("ts") or "")[11:16]
    if ts:
        return ts
    from services.signals import event_moment
    fired = m.get("fired_at")
    if not fired:
        return ""
    return event_moment(fired).astimezone(_MSK).strftime("%H:%M")


def _icon(m: dict, kind: str, side: Optional[str] = None) -> str:
    if kind == "block":
        if m.get("negotiated"):
            return _NDM_ICON
        return _TRADE_ICON.get(m.get("side") or "", _NDM_ICON)
    return _BOOK_ICON.get(side or "", "⚪")


def _issue_link(m: dict) -> str:
    """Имя выпуска ссылкой на его карточку: из чата один тап до стакана."""
    # имена приходят из справочников MOEX — экранируем, иначе один «&» в
    # названии рушит разбор HTML, и Telegram отбивает всё сообщение
    name = html.escape(str(m.get("name") or m.get("isin") or "—"))
    isin = m.get("isin")
    if not isin:
        return f"<b>{name}</b>"
    return f'<a href="{_SITE_URL}?isin={isin}&amp;ob=1"><b>{name}</b></a>'


def _fmt_match(m: dict, kind: str, side: Optional[str] = None) -> str:
    """Две строки на бумагу.

    Первая — то, ради чего сообщение открывают: сторона значком, спред, деньги,
    срок; ИМЯ ВЫПУСКА В КОНЦЕ, потому что оно длинное и разной длины — если
    ставить его первым, числа в ленте сообщений встают рваной лесенкой и их
    не сравнить между собой. Вторая строка — детали: цена, глубина, причина.
    """
    head, sub = [], []

    if m.get("val_bps") is not None:
        head.append(f"<b>{m['val_bps']:.0f} бп</b>")
    money = _short_money(m.get("money_rub"))
    if money:
        head.append(money)
    years = _fmt_years(m.get("years"))
    if years:
        head.append(years)
    head.append(_issue_link(m))

    if m.get("price") is not None:
        sub.append(f"{_num(m['price'])}%")
    if kind == "block":
        ts = _hhmm(m)
        if ts:
            sub.append(ts)
        if m.get("rating"):
            sub.append(str(m["rating"]))
    else:
        if m.get("single_px") is not None:
            sub.append(f"одна заявка {_num(m['single_px'])}")
        elif m.get("levels"):
            sub.append(f"{m['levels']} ур")
        # причина ДЕЛЬТОЙ («объём +6 %»): слово без величины не говорит,
        # стоит ли отрываться от текущего дела
        why = _reason_delta(m) or _REASON.get(m.get("reason") or "", "")
        if why:
            sub.append(why)

    line = f"{_icon(m, kind, side)}  " + " · ".join(b for b in head if b)
    return line + (f"\n{' · '.join(sub)}" if sub else "")


# Сколько уровней стакана прикладывать к заявке. Четыре — компромисс: экран
# телефона, а глубже верха книги сигнал всё равно не про что.
BOOK_LEVELS = int(os.getenv("TG_BOOK_LEVELS", "4"))


def _book_money(v: Optional[float]) -> str:
    """Деньги уровня — в колонку фиксированной ширины (моноширинный блок)."""
    if not v:
        return "".rjust(6)
    unit, scaled = ("м", v / 1e6) if abs(v) >= 1e6 else ("к", v / 1e3)
    txt = _num(scaled, 1)
    if txt.endswith(",0"):
        txt = txt[:-2]
    return f"{txt}{unit}".rjust(6)


def _book_pre(m: dict, side: Optional[str]) -> str:
    """Стакан на момент события — моноширинным блоком под текстом.

    Пока уведомление доедет до телефона, книга успеет поменяться, поэтому
    прикладываем ровно тот снимок, на котором фильтр сработал (см.
    screener_core.book_snapshot). Уровень, по которому считался сигнал,
    помечаем стрелкой: в лестнице из восьми строк своя цена иначе теряется.
    """
    book = m.get("book") or {}
    asks, bids = book.get("asks") or [], book.get("bids") or []
    if not asks and not bids:
        return ""
    px = m.get("single_px") if m.get("single_px") is not None else m.get("price")

    def row(lvl: dict) -> str:
        y = lvl.get("y_idx")
        y_txt = f"{y:.0f}".rjust(4) if y is not None else "   —"
        # своя цена — стрелкой; эмодзи внутри моноширинного блока нельзя,
        # они двойной ширины и рвут колонки
        hit = " ←" if (px is not None and lvl.get("price") is not None
                       and abs(lvl["price"] - px) < 0.005) else ""
        return f"{_num(lvl['price']).rjust(7)} {_book_money(lvl.get('money'))} {y_txt}{hit}"

    lines = [f"{'ЦЕНА':>7} {'ОБЪЁМ':>6} {'СПРД':>4}"]
    lines += [row(l) for l in asks]
    lines.append("─" * 19)          # выше разделителя оффера, ниже биды
    lines += [row(l) for l in bids]
    return "<pre>" + "\n".join(lines) + "</pre>"


def _signal_text(buf: dict) -> str:
    kind = "block" if buf.get("kind") == "block" else "book"
    ms = buf["matches"]
    n = len(ms)
    side_key = buf.get("side")
    if kind == "book":
        # одно сообщение = одна бумага (см. _group): показываем последнее
        # состояние и прикладываем к нему стакан того же такта
        m = ms[-1]
        body = _fmt_match(m, kind, side_key)
        book = _book_pre(m, side_key)
        if book:
            body += "\n" + book
        if n > 1:
            body += f"\n<i>срабатываний за такт: {n}</i>"
    else:
        body = "\n\n".join(_fmt_match(m, kind, side_key) for m in ms[:MAX_MATCHES])
        if n > MAX_MATCHES:
            body += f"\n\n…ещё {n - MAX_MATCHES}"
    # Подпись фильтра — В КОНЦЕ: сверху должно быть само событие, а «кто позвал»
    # это сноска, которую читают, только если событие зацепило.
    if kind == "block":
        foot = f"💥 <b>{buf['name']}</b>" if buf.get("name") else "💥 <b>Крупные сделки</b>"
    else:
        side = {"ask": "оффер", "bid": "бид"}.get(side_key or "", "")
        foot = f"📡 <b>{buf['name']}</b>" + (f" · {side}" if side else "")
    return f"{body}\n\n{foot}"


async def _flush_signals() -> None:
    if not _pending:
        return
    batch = list(_pending.items())
    _pending.clear()
    for (chat_id, _fid, _group_key), buf in batch:
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
