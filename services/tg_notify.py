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
# ключ буфера (chat_id, filter_id, isin) → (message_id, монотонное время)
# последнего сообщения по этой бумаге: следующее уходит ответом на него
_threads: dict = {}
# Сколько живёт нить. Три часа: заявка, о которой не было слышно полсессии, —
# уже другая история, и ответ на неё уводил бы в архив вместо связи.
THREAD_TTL_SEC = float(os.getenv("TG_THREAD_TTL_MIN", "180")) * 60
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
    состояние.

    СДЕЛКИ — по одному сообщению на НАБОР ОДИНАКОВЫХ. Разные сделки склеивать
    нельзя: пачкой они отличаются только объёмом, и глаз ищет разницу вместо
    того, чтобы её увидеть. Но три принта по одной бумаге, по одной цене и в
    одну сторону — это ОДНО событие, разбитое биржей на части: у них общие
    спред, формула и рейтинг, и печатать их тремя одинаковыми карточками
    значит трижды повторить одно и то же. Такие уходят одним сообщением с
    суммарным объёмом и расшифровкой (см. _signal_text)."""
    key = (lambda m: m.get("isin")) if kind == "book" else _trade_key
    groups: dict = {}
    for m in matches:
        groups.setdefault(key(m), []).append(m)
    return list(groups.items())


def _trade_key(m: dict) -> tuple:
    """Что делает две сделки ОДНИМ событием: бумага, цена, сторона агрессора и
    режим торгов.

    Спред и формула из этих полей и следуют, поэтому в ключ не входят. Время в
    ключе тоже не нужно: части одного набора расходятся на доли секунды, а
    сделка получасом позже приедет отдельным сообщением сама — её буфер к тому
    моменту уже слит."""
    return (m.get("isin"), m.get("price"), m.get("side"),
            bool(m.get("negotiated")))


def enqueue_signal(user_email: str, filter_id: int, filter_name: str,
                   side: Optional[str], matches: List[dict],
                   kind: str = "book", target_id: Optional[int] = None) -> None:
    """Из signals.run_cycle / block_trades.notify_blocks — рядом с WS-пушем.
    Складывает в буфер: отправка пачкой из flush-воркера. Никогда не бросает.

    target_id — адресат фильтра (канал/группа из services/tg_targets): «Р5»
    уходит в свой канал, «Ф5» в свой. Без него — во все личные чаты аккаунта,
    как было. Маркеры строк канал берёт у владельца: набор /custom настраивают
    в личке, а канал — просто адрес."""
    if not telegram.enabled() or not matches:
        return
    try:
        chats = tg_users.chats_for_email(user_email)
        target_chat = None
        # КАНАЛ — ЭТО АДРЕС, А НЕ ПРАВО НА ДОСТАВКУ. Право живёт в привязке
        # владельца: tg_targets.chat_id_for проверяет только владение строкой и
        # про статус привязки не знает. Без этой проверки revoke и удаление
        # аккаунта гасили личку, а канал продолжал звонить уволенному.
        # Проверяем БЕЗ учёта mute: /mute — «не пиши мне в личку», каналы он
        # глушить не должен.
        if target_id and tg_users.email_exists_approved(user_email):
            from services import tg_targets
            target_chat = tg_targets.chat_id_for(target_id, user_email)
        if target_chat is not None:
            icons = tg_users.icons(chats[0] if chats else None)
            chats = [{"chat_id": target_chat, "emoji": None, "_icons": icons}]
        if not chats:
            return
        for u in chats:
            for group, ms in _group(matches, kind):
                buf = _pending.setdefault(
                    (u["chat_id"], filter_id, group),
                    {"name": filter_name, "side": side, "kind": kind,
                     "matches": [], "first_ts": time.monotonic()})
                buf["name"], buf["side"], buf["kind"] = filter_name, side, kind
                buf["icons"] = u.get("_icons") or tg_users.icons(u)
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


def _icon(m: dict, kind: str, side: Optional[str] = None,
          icons: Optional[dict] = None) -> str:
    """Маркер строки. icons — набор чата (см. tg_users.icons, команда /custom);
    None — дефолты."""
    ic = icons or {}
    ndm = ic.get("ndm", _NDM_ICON)
    if kind == "block":
        if m.get("negotiated"):
            return ndm
        s = m.get("side") or ""
        return ic.get(s, _TRADE_ICON.get(s, ndm)) if s else ndm
    s = side or ""
    return ic.get(s, _BOOK_ICON.get(s, "⚪")) if s else "⚪"


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
    хвосте строки с цифрами тап попадал бы то в него, то в соседнее число.

    Пусто, если имени выпуска нет: в шапке тогда стоит сам ISIN (сделки идут
    по всему рынку, а справочник имён — только по юниверсу), и второй раз
    печатать его незачем."""
    isin = m.get("isin")
    if not isin or not m.get("name") or str(m["name"]) == str(isin):
        return ""
    return f"<code>{html.escape(str(isin))}</code>"


def _money_of(m: dict, kind: str) -> Optional[float]:
    """Объём в шапке строки.

    У ЗАЯВКИ это НАКОПЛЕННЫЙ объём (`level_money_rub`): вся глубина до цены, на
    которой набор остановился. По средневзвесу мерить нельзя — он лучше худшего
    взятого уровня, и объём выходил меньше самого набора. Два соседних
    кандидата не годятся: `money_rub` в режиме порога равен примерно самому
    порогу («>1 млн» → «1м ₽»), то есть повторяет настройку; `money_ok_rub` без
    границ спреда — это ВСЯ сторона стакана (Газпн3P13R 24.08: 20,7м в шапке
    против 3,8м, доступных по 99,86, где и сработало). Фолбэки — на случай
    событий из ленты, где нового поля нет.
    У СДЕЛКИ объём один и без вариантов."""
    if kind == "block":
        return m.get("money_rub")
    for k in ("level_money_rub", "money_ok_rub", "money_rub"):
        v = m.get(k)
        if v:
            return v
    return None


_BASE_SHORT = {"KEYRATE": "КС", "RUONIA": "RU"}


def _formula(m: dict) -> str:
    """Формула купона одной строкой: «КС + 1,2% (12)» — база, маржа выпуска и
    сколько раз в год платят.

    Стоит под стаканом, потому что отвечает на вопрос, который возникает сразу
    после цифр: чем эта бумага вообще платит. Без базы не пишем ничего —
    «+ 1,2%» само по себе бессмысленно."""
    base = _BASE_SHORT.get(m.get("base") or "")
    if not base:
        return ""
    out = base
    bps = m.get("margin_bps")
    if bps:
        pct = f"{bps / 100:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        out += f" + {pct}%"
    cpy = m.get("cpy")
    if cpy:
        out += f" ({int(cpy)})"
    return out


def _fmt_threshold(v: Optional[float]) -> str:
    """Порог объёма из настроек фильтра: «>1м». Он в строке нужен, чтобы
    сравнивать фактический объём с тем, на что подписан — без него «8,2м» не
    говорит, насколько сильно рынок перекрыл условие."""
    return f">{_compact(v)}" if v else ""


def _match_parts(m: dict, kind: str, side: Optional[str] = None,
                 icons: Optional[dict] = None) -> tuple:
    """Запись о бумаге четырьмя частями: (шапка, цена, детали, хвост).

    Разложено, а не склеено, потому что между второй и третьей частью встаёт
    СТАКАН: сверху — ради чего открывают сообщение (спред, выпуск, цена,
    объём), дальше книга, а «чем платит и что копировать» читают уже после
    неё. Хвост — обстоятельства срабатывания (уровни, причина, время, порог):
    он уезжает в подпись сообщения, к имени фильтра, потому что это уже не про
    бумагу, а про то, кто и когда позвал."""
    head, sub, foot = [], [], []

    # Порядок шапки: спред → выпуск со сроком. Спред первым, потому что по
    # нему решают, стоит ли читать дальше; срок в скобках при имени, потому
    # что «180 бп» у годовой бумаги и у пятилетней — разные новости.
    if m.get("val_bps") is not None:
        head.append(f"<b>{m['val_bps']:.0f} бп</b>")
    years = _fmt_years(m.get("years"))
    head.append(_issue_link(m) + (f" ({years})" if years else ""))

    # Цена и объём — ЖИРНЫМ: это две цифры, ради которых сообщение открывают,
    # остальное в строке им подпись.
    money = _short_money(_money_of(m, kind))
    px = [f"<b>{_num(m['price'])}%</b>"] if m.get("price") is not None else []

    formula = _formula(m)
    if formula:
        sub.append(formula)
    if kind == "block":
        # у сделки объём замыкает шапку: он и есть событие («кто-то взял на
        # 1,9м»), цена с рейтингом читаются уже под ним
        if money:
            head.append(f"<b>{money}</b>")
        if m.get("rating"):
            sub.append(html.escape(str(m["rating"])))
        ts = _hhmmss(m)
        if ts:
            foot.append(ts)
    else:
        # у заявки объём — накопленная глубина ДО этой цены, поэтому стоит
        # рядом с ней, а не в шапке: «почём и на сколько» — одна мысль
        if m.get("rating"):
            px.append(html.escape(str(m["rating"])))
        if money:
            px.append(f"<b>{money}</b>")
        if m.get("single_px") is not None:
            foot.append(f"одна заявка {_num(m['single_px'])}")
        elif m.get("levels"):
            foot.append(f"{m['levels']} ур")
        # причина ДЕЛЬТОЙ («объём +6 %»): слово без величины не говорит,
        # стоит ли отрываться от текущего дела
        why = _reason_delta(m) or _REASON.get(m.get("reason") or "", "")
        if why:
            foot.append(why)
        ts = _hhmmss(m)
        if ts:
            foot.append(ts)
        want = _fmt_threshold(m.get("want_money_rub"))
        if want:
            foot.append(want)

    # ISIN замыкает детали: его копируют целиком, и в конце строки тап по нему
    # не спорит с соседними числами
    isin = _isin_line(m)
    if isin:
        sub.append(isin)

    head_line = f"{_icon(m, kind, side, icons)}  " + " · ".join(b for b in head if b)
    return (head_line, " · ".join(px), " · ".join(b for b in sub if b),
            " · ".join(b for b in foot if b))


def _fmt_match(m: dict, kind: str, side: Optional[str] = None,
               icons: Optional[dict] = None, with_foot: bool = True) -> str:
    """Запись без стакана — сделки идут пачкой, книгу к ним не прикладываем.

    with_foot=False, когда время уезжает в подпись сообщения (одна сделка на
    сообщение): дважды его печатать незачем."""
    head, px, details, foot = _match_parts(m, kind, side, icons)
    top = "\n".join(p for p in (head, px) if p)
    parts = [top, details] + ([foot] if with_foot else [])
    return "\n\n".join(p for p in parts if p)


# Сколько уровней стакана прикладывать к заявке. Четыре — компромисс: экран
# телефона, а глубже верха книги сигнал всё равно не про что.
BOOK_LEVELS = int(os.getenv("TG_BOOK_LEVELS", "4"))


# ВЫРАВНИВАНИЕ БЕЗ МОНОШИРИННОГО ШРИФТА. Лестница набирается обычным шрифтом
# (моноширинный <code> давал терминальный вид), а колонки держатся на U+2007
# FIGURE SPACE — это пробел шириной РОВНО В ЦИФРУ. Обычный пробел ýже цифры,
# и на нём колонки разъезжались тем сильнее, чем разнее длина чисел
# («334» против «67 892»). Тем же символом разделяются разряды в количестве.
_FIG = "\u2007"
_GAP = _FIG * 3                 # промежуток между колонками — три цифры
_W_PX, _W_QTY, _W_RS = 6, 7, 4  # цена / штуки / спред


def _pad(s: str, w: int) -> str:
    return _FIG * max(0, w - len(s)) + s


def _book_qty(lvl: dict) -> str:
    """Объём уровня В БУМАГАХ — в колонку фиксированной ширины.

    Штуки, а не рубли: в стакане торгуют количеством, а рублёвый эквивалент
    уже стоит в шапке сообщения (накопленный объём). Событиям из старого
    буфера, где qty ещё нет, колонка достаётся пустой — но не рублями, иначе
    два разных смысла в одной колонке."""
    v = lvl.get("qty")
    if not v:
        return _pad("", _W_QTY)
    # до сотни тысяч — точное число («38», «24 950»): в стакане облигаций это
    # обычный размер заявки, и «0к» вместо него бесполезно. Крупнее — порядок
    # («120к»), иначе колонка не влезает в экран телефона.
    txt = (f"{v:,.0f}".replace(",", _FIG) if abs(v) < 100_000 else _compact(v))
    return _pad(txt, _W_QTY)


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

    # Сколько уровней СЪЕЛ набор: спред в шапке посчитан по их средневзвесу, и
    # такой цены в книге нет ни одной строкой. Без пометки сообщение читается
    # как «в шапке 162, а в стакане 172» — то самое расхождение, из-за которого
    # цифрам перестают верить (жалоба 27.08.2026).
    taken = m.get("levels") if m.get("single_px") is None else None

    # Цены, ПО КОТОРЫМ посчитан сигнал: у одиночной заявки — своя, у набора —
    # первые taken уровней стороны сигнала, считая ОТ ЛУЧШЕГО. Помечаем по
    # цене, а не по номеру строки: порядок сторон в лестнице биржевой, и
    # индекс в ней не совпадает с порядком набора.
    near_best = list(bids) if side == "bid" else list(reversed(asks))
    if taken:
        hit_px = [l["price"] for l in near_best[:taken] if l.get("price") is not None]
    else:
        hit_px = [px] if px is not None else []

    def row(lvl: dict) -> str:
        y = lvl.get("y_idx")
        y_txt = _pad(f"{y:.0f}" if y is not None else "—", _W_RS)
        # своя цена — стрелкой, а не эмодзи: цветные символы двойной ширины
        # рвут колонку
        p = lvl.get("price")
        hit = " ←" if (p is not None
                       and any(abs(p - h) < 0.005 for h in hit_px)) else ""
        return _GAP.join((_pad(_num(lvl["price"]), _W_PX),
                          _book_qty(lvl), y_txt)) + hit

    # ПОРЯДОК — биржевой, как в любом терминале: офферы сверху вниз до лучшего,
    # под чертой биды от лучшего вниз, цена по столбцу монотонно падает. Ставили
    # «сторона сигнала первой» ради двух строк, видимых под свёрнутой цитатой,
    # но перевёрнутая лестница читается неверно вся целиком, а цена и спред
    # сигнала и так стоят в шапке сообщения — над цитатой.
    # Строки заголовка НЕТ: «ЦЕНА ШТ RS» набирается буквами, а буквы в обычном
    # шрифте разной ширины — шапка вставала над колонками на глаз и портила
    # ровно то, ради чего колонки и держим. Колонки узнаются без подписи:
    # цена, количество, спред.
    lines = [row(l) for l in asks]
    lines.append("─" * 20)          # выше разделителя оффера, ниже биды
    lines += [row(l) for l in bids]
    # Сворачиваемая цитата: в ленте чата стакан не занимает экран, но
    # разворачивается одним тапом. Моноширинный <code> снят — выравнивание
    # держит FIGURE SPACE (см. выше), а обычный шрифт читается как текст
    # сообщения, а не как вывод терминала.
    body = "\n".join(lines)
    return f"<blockquote expandable>{body}</blockquote>"


def _breakdown(ms: List[dict]) -> str:
    """Из чего сложился суммарный объём: «3 сделки · 198м · 47,8м · 6,4м».

    Тотал отвечает «сколько взяли», расшифровка — «одним куском или мелочью»:
    198 млн одним принтом и те же 198 млн двадцатью кусками говорят о рынке
    разное. Мелкой строкой и ПОД параметрами: это уточнение к цифре из шапки,
    а не самостоятельная новость. Длинный хвост сворачиваем — на экране
    телефона два десятка чисел всё равно не читаются."""
    if len(ms) < 2:
        return ""
    vals = sorted((x.get("money_rub") or 0 for x in ms), reverse=True)
    shown = [_compact(v) for v in vals[:MAX_MATCHES]]
    if len(vals) > MAX_MATCHES:
        shown.append(f"…ещё {len(vals) - MAX_MATCHES}")
    return f"<i>{len(ms)} {_trades_word(len(ms))} · " + " · ".join(shown) + "</i>"


def _trades_word(n: int) -> str:
    """«2 сделки», «5 сделок» — падеж в строке, которую читают каждый день,
    важнее краткости кода."""
    if 11 <= n % 100 <= 14:
        return "сделок"
    last = n % 10
    if last == 1:
        return "сделка"
    return "сделки" if 2 <= last <= 4 else "сделок"


def _trade_time(ms: List[dict]) -> str:
    """Время набора: момент — если сделки в одну секунду, иначе интервал.

    Одна отметка на растянутый набор врала бы: «взяли 250 млн в 15:40:33»
    читается как один принт, хотя набирали три минуты."""
    stamps = [t for t in (_hhmmss(m) for m in ms) if t]
    if not stamps:
        return ""
    lo, hi = min(stamps), max(stamps)
    return lo if lo == hi else f"{lo}–{hi}"


def _signal_text(buf: dict) -> str:
    kind = "block" if buf.get("kind") == "block" else "book"
    ms = buf["matches"]
    n = len(ms)
    side_key = buf.get("side")
    # маркеры чата кладёт enqueue_signal: набор известен там, где известен
    # адресат, и доставка не ходит за ним в базу на каждое сообщение
    icons = buf.get("icons")
    extra = ""
    if kind == "book":
        # одно сообщение = одна бумага (см. _group): показываем последнее
        # состояние и прикладываем к нему стакан того же такта
        m = ms[-1]
        head, px, details, extra = _match_parts(m, kind, side_key, icons)
        book = _book_pre(m, side_key)
        if n > 1:
            extra = ((extra + " · ") if extra else "") \
                + f"<i>срабатываний за такт: {n}</i>"
        # стакан ВНУТРИ записи: сразу под ценой, до формулы с ISIN. Шапка от
        # цены отбита пустой строкой — иначе спред, выпуск и цифры сливаются
        # в одну простыню, и глазу негде остановиться.
        top = "\n\n".join(p for p in (head, px) if p)
        body = "\n".join(p for p in (top, book, details) if p)
    else:
        # набор одинаковых сделок (см. _group и _trade_key) — одна карточка с
        # СУММАРНЫМ объёмом: параметры у них общие, и повторять их построчно
        # значит заставлять читателя сверять три одинаковых блока
        m = dict(ms[-1])
        if n > 1:
            m["money_rub"] = sum(x.get("money_rub") or 0 for x in ms)
        head, px, details, _foot = _match_parts(m, kind, side_key, icons)
        body = "\n\n".join(p for p in ("\n".join((head, px)).strip(),
                                       details, _breakdown(ms)) if p)
        extra = _trade_time(ms)
    # Подпись фильтра — В КОНЦЕ: сверху должно быть само событие, а «кто позвал,
    # почему и когда» это сноска, которую читают, только если событие зацепило.
    if kind == "block":
        # имя алерта КУРСИВОМ: это сноска «кто позвал», и жирный спорил с
        # ценой и объёмом — единственным, что должно тянуть взгляд
        foot = (f"<i>{html.escape(str(buf['name']))}</i>" if buf.get("name")
                else "<i>Крупные сделки</i>")
    else:
        side = {"ask": "оффер", "bid": "бид"}.get(side_key or "", "")
        foot = (f"<i>{html.escape(str(buf['name']))}</i>"
                + (f" · {side}" if side else ""))
    if extra:
        foot += f" · {extra}"
    return f"{body}\n\n{foot}"


def _thread_anchor(key: tuple, buf: dict, now: float) -> Optional[int]:
    """На какое сообщение отвечать этим сигналом.

    Повтор по бумаге (спред уехал, объём набрался) уходит ОТВЕТОМ на прошлое
    сообщение о ней: в чате получается история одной заявки, а не десяток
    одинаковых карточек, между которыми глазом ищешь, что изменилось.
    Нить ведётся по ключу буфера (чат, фильтр, выпуск) — у сделок выпуска в
    ключе нет, они идут пачкой, и отвечать там не на что.

    Нить начинается заново на «заявке» (первое попадание бумаги под условия):
    уровень пропал и появился снова — это новая история. Просроченная нить
    тоже начинается заново: ответ на утреннее сообщение к вечеру уводит
    читателя в архив, а не показывает связь."""
    if buf.get("kind") != "book" or key[2] is None:
        return None
    mid, ts = _threads.get(key, (None, 0.0))
    if not mid or now - ts > THREAD_TTL_SEC:
        return None
    ms = buf.get("matches") or []
    if ms and (ms[-1].get("reason") or "") == "new":
        return None
    return mid


def _remember_thread(key: tuple, res: Optional[dict], buf: dict, now: float) -> None:
    """Запоминает отправленное сообщение как хвост нити по бумаге."""
    if buf.get("kind") != "book" or key[2] is None or not res:
        return
    mid = res.get("message_id")
    if mid:
        _threads[key] = (int(mid), now)


def _prune_threads(now: float) -> None:
    """Чистит протухшие нити: словарь живёт всё время процесса, а бумаг за день
    проходят сотни."""
    for k in [k for k, (_, ts) in _threads.items() if now - ts > THREAD_TTL_SEC]:
        _threads.pop(k, None)


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


# Сколько держим неотправленное. Прокси-сайдкар и Bot API падают на минуты, а
# не на часы; сигнал старше этого срока уже не новость, и слать его вдогонку —
# только путать. Пока держим — доставим, как только канал вернётся.
REQUEUE_MAX_SEC = float(os.getenv("TG_REQUEUE_MAX_SEC", "600"))


def _requeue(key: tuple, buf: dict, now: float) -> None:
    """Возвращает неотправленное в буфер. Раньше отправка чистила очередь ДО
    результата, и отказ канала (упавший прокси, 5xx Bat API) стирал сигналы
    бесследно: в вебе они есть, в телеграм не придут никогда."""
    age = now - buf.get("first_ts", now)
    if age > REQUEUE_MAX_SEC:
        logger.warning("tg_notify: сигнал по %s брошен — канал молчит %.0f мин",
                       buf.get("name") or key, age / 60)
        return
    old = _pending.get(key)
    if old:                      # пока ждали, накопилось новое — склеиваем
        old["matches"] = (buf.get("matches", []) + old.get("matches", []))[-40:]
        old["first_ts"] = min(old.get("first_ts", now), buf.get("first_ts", now))
    else:
        _pending[key] = buf
    # чат снова считается «молчавшим»: иначе окно коалесценции задержало бы
    # повторную попытку ещё на такт
    _last_sent.pop(key[0], None)


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

    _prune_threads(now)

    async def send(key: tuple, buf: dict) -> None:
        chat_id = key[0]
        async with sem:
            try:
                res = await telegram.send_message(
                    chat_id, _signal_text(buf),
                    reply_to=_thread_anchor(key, buf, now))
            except Exception as e:
                logger.warning("tg_notify signal send error (chat %s): %s", chat_id, e)
                res = None
            if res is None:
                _requeue(key, buf, now)
                return
            _remember_thread(key, res, buf, now)

    # Чаты параллельно: сериальный цикл складывал RTT прокси на каждое
    # сообщение, и хвост пачки приезжал заметно позже головы.
    await asyncio.gather(*(send(key, buf) for key, buf in batch))


# --- служебные предупреждения ---

async def notify_admins(text: str) -> int:
    """Служебное сообщение владельцам системы. → сколько чатов получило.

    Отдельный путь от сигналов: это не про рынок, а про то, что прибор
    сломался, и адресат тут не «кто подписался на фильтр», а «кто чинит».
    Поэтому и буфера нет — предупреждение не имеет смысла коалесцировать, оно
    и так редкое, а задержка в десять секунд у него дороже.

    Молчит, если админам не привязан чат: заводить доставку некуда, а падать
    из-за этого сторожу нельзя."""
    from services import auth_users
    if not telegram.enabled():
        return 0
    try:
        chats = {c["chat_id"] for u in auth_users.list_users()
                 if u.get("role") == "admin"
                 for c in tg_users.chats_for_email(u["email"])}
    except Exception as e:
        logger.warning("notify_admins: не собрать адресатов: %s", e)
        return 0
    sent = 0
    for chat_id in chats:
        try:
            if await telegram.send_message(chat_id, text):
                sent += 1
        except Exception as e:
            logger.warning("notify_admins send error (chat %s): %s", chat_id, e)
    return sent


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
