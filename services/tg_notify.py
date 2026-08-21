"""Доставка в Telegram событий вкладки СИГНАЛЫ (фильтры стакана и крупные
сделки). Своей настройки у бота нет — адресат ищется по user_email через
привязку чата (services/tg_users.py).

События копятся в буфере, но окно коалесценции работает ТОЛЬКО против серии:
первое событие по тихому чату уходит сразу (см. _due), а дальше по этому чату
сообщения склеиваются на TG_SIGNAL_FLUSH_SEC. Тик скринера секундный, и
поштучная отправка серии выбила бы лимиты Bot API и превратила чат в ленту.
Ошибки доставки логируются и глотаются: веб-механика (лента событий) уже
отработала."""
import asyncio
import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from services import telegram, tg_users

logger = logging.getLogger(__name__)

# (chat_id, filter_id, group) → {name, side, kind, matches, first_ts} — буфер
# коалесценции сигналов
_pending: dict = {}
# chat_id → монотонное время последней отправки: по нему считается «тишина»
_last_sent: dict = {}
SIGNAL_FLUSH_SEC = float(os.getenv("TG_SIGNAL_FLUSH_SEC", "10"))
# Тик воркера: окно проверяется чаще, чем длится, иначе «сразу» превращается
# в «в пределах окна» и весь смысл раннего флаша теряется.
FLUSH_TICK_SEC = float(os.getenv("TG_SIGNAL_TICK_SEC", "1"))
# Сколько сообщений в один чат за такт. Bot API терпит короткий всплеск, но
# устойчиво держит ~1 msg/сек на чат — остаток ждёт следующего такта.
MAX_BURST = int(os.getenv("TG_SIGNAL_BURST", "5"))
# Параллельность отправки между чатами: сериальный цикл упирался в RTT прокси.
SEND_CONCURRENCY = int(os.getenv("TG_SEND_CONCURRENCY", "8"))
MAX_MATCHES = 8              # в одном сообщении; остальное сворачиваем в «ещё N»

_MSK = timezone(timedelta(hours=3))
_REASON = {"new": "заявка", "price": "цена", "spread": "RS", "money": "объём",
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
        pct = f"{(cur - prev) / abs(prev) * 100.0:+.0f}".replace("-", "−")
        # прежнее значение зачёркнутым: видно, откуда пришли, и не надо считать
        # проценты в уме от текущего числа
        return f"объём {pct} % (<s>{_compact(prev)}</s>)"
    if r == "spread":
        prev, cur = m.get("prev_val_bps"), m.get("val_bps")
        if prev is None or cur is None:
            return "RS"
        num = f"{cur - prev:+.0f}".replace("-", "−")
        return f"RS {num} бп (<s>{prev:.0f}</s>)"
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
                    {"name": filter_name, "side": side, "kind": kind,
                     "matches": [], "first_ts": time.monotonic()})
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


def _compact(v: Optional[float]) -> str:
    """«1м», «26,1м», «300к» — порядок суммы без валюты, для тесных мест."""
    if not v:
        return "0"
    unit, scaled = ("м", v / 1e6) if abs(v) >= 1e6 else ("к", v / 1e3)
    txt = _num(scaled, 1)
    return (txt[:-2] if txt.endswith(",0") else txt) + unit


def _short_money(v: Optional[float]) -> str:
    """Деньги коротко: «1м ₽», «26,1м ₽», «300к ₽». В строке сигнала важен
    порядок суммы, а не копейки — длинное «26,1 млн ₽» съедает место у цифр,
    ради которых сообщение и открывают."""
    if not v:
        return ""
    return f"{_compact(v)} ₽"      # «1,0м» читается хуже, чем «1м» — см. _compact


def _hhmmss(m: dict) -> str:
    """Время сделки, МСК, С СЕКУНДАМИ: у крупных принтов важна очерёдность
    внутри минуты — по минуте не понять, кто кого перебил.

    В живом пуше приходит ts самой сделки; в строке из ленты его нет (в таблице
    событий хранится только fired_at), поэтому берём момент срабатывания и
    переводим из UTC."""
    ts = (m.get("ts") or "")[11:19]
    if ts:
        return ts
    from services.signals import event_moment
    fired = m.get("fired_at")
    if not fired:
        return ""
    return event_moment(fired).astimezone(_MSK).strftime("%H:%M:%S")


def _icon(m: dict, kind: str, side: Optional[str] = None) -> str:
    if kind == "block":
        if m.get("negotiated"):
            return _NDM_ICON
        return _TRADE_ICON.get(m.get("side") or "", _NDM_ICON)
    return _BOOK_ICON.get(side or "", "⚪")


def _issue_link(m: dict) -> str:
    """Имя выпуска моноширинным.

    Без ссылки на карточку: в Telegram <code> — это tap-to-copy, и из чата
    чаще нужен сам текст (вбить в терминал, найти в таблице), чем переход на
    сайт. Моноширинный шрифт заодно ставит имена в колонку — в ленте сообщений
    их сравнивают глазом, а не читают по одному."""
    # имена приходят из справочников MOEX — экранируем, иначе один «&» в
    # названии рушит разбор HTML, и Telegram отбивает всё сообщение
    name = html.escape(str(m.get("name") or m.get("isin") or "—"))
    return f"<code>{name}</code>"


def _isin_line(m: dict) -> str:
    """ISIN ОТДЕЛЬНОЙ строкой, последней в записи: его копируют целиком, а в
    хвосте строки с цифрами тап попадал бы то в него, то в соседнее число."""
    isin = m.get("isin")
    return f"<code>{html.escape(str(isin))}</code>" if isin else ""


def _money_of(m: dict, kind: str) -> Optional[float]:
    """Объём в шапке строки.

    У ЗАЯВКИ это деньги уровней, попавших в диапазон спреда фильтра
    (money_ok_rub), а не набранный объём: в режиме порога money_rub равен
    примерно самому порогу («>1 млн» → «1м ₽»), то есть сообщение повторяло
    настройку вместо того, чтобы показывать рынок. Фолбэк на money_rub —
    фильтры без границ спреда, где money_ok не считается.
    У СДЕЛКИ объём один и без вариантов."""
    if kind == "block":
        return m.get("money_rub")
    ok = m.get("money_ok_rub")
    return ok if ok else m.get("money_rub")


def _fmt_threshold(v: Optional[float]) -> str:
    """Порог объёма из настроек фильтра: «>1м». Он в строке нужен, чтобы
    сравнивать фактический объём с тем, на что подписан — без него «8,2м» не
    говорит, насколько сильно рынок перекрыл условие."""
    return f">{_compact(v)}" if v else ""


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
    money = _short_money(_money_of(m, kind))
    if money:
        head.append(money)
    years = _fmt_years(m.get("years"))
    if years:
        head.append(years)
    head.append(_issue_link(m))

    if m.get("price") is not None:
        sub.append(f"{_num(m['price'])}%")
    if kind == "block":
        # время — ПОСЛЕ рейтинга: слева то, чем сделку оценивают, справа
        # отметка времени, по которой её потом ищут в ленте
        if m.get("rating"):
            sub.append(str(m["rating"]))
        ts = _hhmmss(m)
        if ts:
            sub.append(ts)
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
        ts = _hhmmss(m)
        if ts:
            sub.append(ts)
        want = _fmt_threshold(m.get("want_money_rub"))
        if want:
            sub.append(want)

    line = f"{_icon(m, kind, side)}  " + " · ".join(b for b in head if b)
    if sub:
        line += f"\n{' · '.join(sub)}"
    isin = _isin_line(m)
    return line + (f"\n{isin}" if isin else "")


# Сколько уровней стакана прикладывать к заявке. Четыре — компромисс: экран
# телефона, а глубже верха книги сигнал всё равно не про что.
BOOK_LEVELS = int(os.getenv("TG_BOOK_LEVELS", "4"))


def _book_money(v: Optional[float]) -> str:
    """Деньги уровня — в колонку фиксированной ширины (моноширинный блок)."""
    if not v:
        return "".rjust(6)
    return _compact(v).rjust(6)


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
        y_txt = f"{y:.0f}".rjust(5) if y is not None else "    —"
        # своя цена — стрелкой; эмодзи внутри моноширинного блока нельзя,
        # они двойной ширины и рвут колонки
        hit = " ←" if (px is not None and lvl.get("price") is not None
                       and abs(lvl["price"] - px) < 0.005) else ""
        return f"{_num(lvl['price']).rjust(7)} {_book_money(lvl.get('money'))} {y_txt}{hit}"

    lines = [f"{'ЦЕНА':>7} {'ОБЪЁМ':>6} {'RS':>5}"]
    lines += [row(l) for l in asks]
    lines.append("─" * 20)          # выше разделителя оффера, ниже биды
    lines += [row(l) for l in bids]
    # Сворачиваемая цитата: в ленте чата стакан не занимает экран, но
    # разворачивается одним тапом. Моноширинность держим построчным <code> —
    # блочный <pre> внутри цитаты Telegram не разрешает.
    body = "\n".join(f"<code>{ln}</code>" for ln in lines)
    return f"<blockquote expandable>{body}</blockquote>"


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
        foot = f"<b>{buf['name']}</b>" if buf.get("name") else "<b>Крупные сделки</b>"
    else:
        side = {"ask": "оффер", "bid": "бид"}.get(side_key or "", "")
        foot = f"<b>{buf['name']}</b>" + (f" · {side}" if side else "")
    return f"{body}\n\n{foot}"


def _due(now: float) -> list:
    """Какие ключи буфера пора отправлять.

    Правило одно: чат, молчавший дольше окна, получает событие НЕМЕДЛЕННО —
    редкий сигнал не должен ждать таймер, ради которого окно и заводилось.
    Как только по чату прошла отправка, следующие события копятся до конца
    окна: серия схлопывается, лимиты Bot API целы. Всплеск режем MAX_BURST,
    остаток доедет следующим тактом (буфер продолжает коалесцировать)."""
    ready: list = []
    for key, buf in _pending.items():
        chat_id = key[0]
        quiet = now - _last_sent.get(chat_id, float("-inf")) >= SIGNAL_FLUSH_SEC
        if quiet or now - buf.get("first_ts", now) >= SIGNAL_FLUSH_SEC:
            ready.append(key)
    if MAX_BURST > 0:
        per_chat: dict = {}
        capped = []
        for key in ready:
            n = per_chat.get(key[0], 0)
            if n >= MAX_BURST:
                continue
            per_chat[key[0]] = n + 1
            capped.append(key)
        ready = capped
    return ready


async def _flush_signals() -> None:
    if not _pending:
        return
    now = time.monotonic()
    keys = _due(now)
    if not keys:
        return
    batch = [(k, _pending.pop(k)) for k in keys]
    for key, _buf in batch:
        _last_sent[key[0]] = now

    sem = asyncio.Semaphore(max(1, SEND_CONCURRENCY))

    async def send(chat_id: int, buf: dict) -> None:
        async with sem:
            try:
                await telegram.send_message(chat_id, _signal_text(buf))
            except Exception as e:
                logger.warning("tg_notify signal send error (chat %s): %s", chat_id, e)

    # Чаты параллельно: сериальный цикл складывал RTT прокси на каждое
    # сообщение, и хвост пачки приезжал заметно позже головы.
    await asyncio.gather(*(send(key[0], buf) for key, buf in batch))


# --- воркеры ---

async def tg_signal_worker() -> None:
    """Фон: частым тиком сливает готовые ключи буфера в чаты (см. _due)."""
    logger.info("tg_signal worker started (window=%.0fs, tick=%.0fs)",
                SIGNAL_FLUSH_SEC, FLUSH_TICK_SEC)
    while True:
        # флаш ПЕРЕД сном: иначе событие, пришедшее сразу после такта, ждало бы
        # полный интервал даже по тихому чату
        try:
            await _flush_signals()
        except Exception as e:
            logger.warning("tg_notify flush error: %s", e)
        await asyncio.sleep(FLUSH_TICK_SEC)
