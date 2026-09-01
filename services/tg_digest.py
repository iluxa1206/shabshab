"""«Разбор дня» — альбом картинок в Telegram после закрытия рынка.

Сигналы говорят про мгновение («вот сейчас кто-то встал широко»), и за день их
набегает столько, что общая картина в ленте не собирается. Дайджест отвечает на
другие вопросы: куда за сегодня уехали спреды, где был оборот, кто переложил
крупный тикет, что сделала кривая и сколько денег придёт на неделе.

Один альбом, а не пять сообщений: в чате это одна карточка, которую листают,
и она не разрывает ленту сигналов на части. Следом уходит короткое сообщение
с кнопками — у медиагруппы своей клавиатуры не бывает (Bot API не принимает
reply_markup в sendMediaGroup), а уйти из картинки в приложение надо.

По пятницам — тот же альбом в недельном окне («Итоги недели»): движения
считаются к прошлой пятнице, обороты суммируются за неделю, в подписи
появляются свежие выпуски.

Данные берём ИЗ УЖЕ ПОСЧИТАННОГО (bar_daily, архив блоков и своп-котировок,
календарь выплат): дайджест — витрина, а не ещё один расчётный слой, и падать
он не имеет права даже если какой-то сюжет пуст (картинка тогда честно скажет,
что данных нет)."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from services import charts_png as ch
from services import telegram, tg_links, tg_users

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

# Порог оборота бумаги, ниже которого её движение спреда — не новость, а шум
# одной случайной сделки на пару лотов.
MIN_VALUE_RUB = float(os.getenv("DIGEST_MIN_VALUE", "3000000"))
MOVERS_SIDE = int(os.getenv("DIGEST_MOVERS", "6"))    # столько вверх и столько вниз
TURNOVER_TOP = int(os.getenv("DIGEST_TURNOVER_TOP", "12"))
BLOCKS_TOP = int(os.getenv("DIGEST_BLOCKS_TOP", "10"))
BLOCKS_PER_ISIN = int(os.getenv("DIGEST_BLOCKS_PER_ISIN", "2"))
PAYMENT_DAYS = int(os.getenv("DIGEST_PAYMENT_DAYS", "10"))
PAYMENT_DAYS_WEEK = int(os.getenv("DIGEST_PAYMENT_DAYS_WEEK", "14"))
CURVE_LOOKBACK_DAYS = int(os.getenv("DIGEST_CURVE_BACK", "7"))
# Сколько торговых дней истории поднимаем ради «серии»: столбик едет третий день
# подряд — это тренд, а не разовый прыжок. Восемь сессий хватает: серия длиннее
# полутора недель в подписи всё равно не читается.
STREAK_DAYS = int(os.getenv("DIGEST_STREAK_DAYS", "8"))
# Недельное окно — в ТОРГОВЫХ днях: считать по календарю нельзя, праздничная
# неделя тогда сравнивала бы пятницу с самой собой.
WEEK_SESSIONS = int(os.getenv("DIGEST_WEEK_SESSIONS", "5"))
NEW_ISSUES_TOP = int(os.getenv("DIGEST_NEW_ISSUES", "5"))
# Выбрасывать ли из рейтинга сделок технику размещения. Первичка идёт через
# РПС по 100,00 на десятки миллиардов и в топе дня забивает собой весь рынок,
# хотя новостью не является: выпуск разместился, а не «кто-то переложился».
SKIP_PLACEMENT = os.getenv("DIGEST_SKIP_PLACEMENT", "1") not in ("0", "false", "no")
# Санитарные границы. В дневной свёртке попадаются бумаги с битым номиналом или
# экзотикой, у которых «премия» выходит в тысячи процентов; в рейтинге движений
# такая строка занимает весь масштаб и прячет настоящие движения рынка.
SANE_SPREAD_BPS = float(os.getenv("DIGEST_SANE_SPREAD", "3000"))
# Нижняя граница премии. Со входом фиксов в дайджест в выборку попали
# структурные ноты (ИОС и подобное): купона у них по сути нет, g-спред выходит
# в минус на тысячи бп, и такая бумага занимала первое место «сильнее всех
# сжался». Премия ниже −300 бп — не рынок, а негодная модель для инструмента.
SANE_MIN_BPS = float(os.getenv("DIGEST_SANE_MIN", "-300"))
SANE_DELTA_BPS = float(os.getenv("DIGEST_SANE_DELTA", "1000"))

# Классы рынка, по одному альбому на каждый. Раньше альбом был общий, а рынки
# разводились внутри картинок; но премия у них считается разными метриками,
# кривая своя, календарь выплат есть только у флоатеров — на общей карточке это
# всё равно оставалось двумя разговорами в одном. Валютные (FACEUNIT ≠ SUR) в
# свёртке пока не появляются вовсе: их не собирает стрим, отдельная задача.
SCOPES = {
    "floater": {"title": "Флоатеры", "metric": "Y-IDX", "chip": "🟦",
                "curve": "KEYRATE", "payments": True},
    "fixed": {"title": "Фиксы", "metric": "g-спред", "chip": "🟪",
              "curve": None, "payments": False},
}
DEFAULT_SCOPES = [x.strip() for x in
                  (os.getenv("DIGEST_SCOPES") or "floater,fixed").split(",") if x.strip()]

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря")
_TENOR_ORDER = ("ON", "1W", "2W", "1M", "2M", "3M", "6M", "9M",
                "1Y", "2Y", "3Y", "5Y", "7Y", "10Y")


def _ru_date(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _label_date(iso: Optional[str], fallback: str) -> str:
    """Дата среза кривой по-русски: в легенде «7 августа» читается, а ISO —
    сверяется по цифрам."""
    try:
        return _ru_date(date.fromisoformat(iso))
    except Exception:
        return fallback


# ── сбор данных (SQLite, синхронно — зовётся из потока) ────────────────────
def _last_days(limit: int) -> List[str]:
    """Последние торговые дни свёртки, свежий первым."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute("SELECT DISTINCT date FROM bar_daily "
                         "ORDER BY date DESC LIMIT ?", (max(1, limit),)).fetchall()
    return [r["date"] for r in rows]


def _day_rows(day: str) -> List[dict]:
    from services.portfolio_db import _connect
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT isin, kind, y_idx_close_bps, g_spread_close_bps, close_pct, "
            "value, trades FROM bar_daily WHERE date=?", (day,))]


def _spread_history(days: List[str]) -> dict:
    """{isin: {день: спред}} по списку дней — одним запросом.

    Нужна только для серий, поэтому тянем две колонки спреда и ничего больше:
    поднимать полные строки за восемь дней ради знака дельты — лишняя работа."""
    if not days:
        return {}
    from services.portfolio_db import _connect
    q = ("SELECT isin, date, y_idx_close_bps, g_spread_close_bps FROM bar_daily "
         f"WHERE date IN ({','.join('?' * len(days))})")
    out: dict = {}
    with _connect() as c:
        for r in c.execute(q, days):
            v = r["y_idx_close_bps"]
            v = v if v is not None else r["g_spread_close_bps"]
            if v is not None:
                out.setdefault(r["isin"], {})[r["date"]] = float(v)
    return out


def _streak(hist: dict, days: List[str], sign: float) -> int:
    """Сколько торговых дней подряд спред едет в ту же сторону, что сегодня.

    Считаем по ЗАКРЫТИЯМ подряд идущих дней свёртки: дырка в истории (бумага
    не торговалась) обрывает серию — «третий день подряд» про бумагу, которую
    видели раз в неделю, был бы враньём."""
    if not hist or sign == 0:
        return 0
    n = 0
    for i in range(len(days) - 1):
        cur, prev = hist.get(days[i]), hist.get(days[i + 1])
        if cur is None or prev is None:
            break
        d = cur - prev
        if d == 0 or (d > 0) != (sign > 0):
            break
        n += 1
    return n


def _placement_dates() -> dict:
    """{isin: дата размещения} по свежим выпускам — чтобы узнать первичку.

    Смотрим только недавние: сделка «в день размещения» физически возможна
    лишь у бумаги, размещённой на этой же неделе, а поднимать даты по всему
    реестру ради одного сюжета незачем."""
    try:
        from services import instruments_registry as reg
        return {r["isin"]: (r.get("issue_date") or "")[:10]
                for r in reg.list_new_issues(days=45) if r.get("issue_date")}
    except Exception as e:
        logger.warning("digest: даты размещения недоступны: %s", e)
        return {}


def _blocks(days: List[str], kinds: Optional[dict] = None) -> List[dict]:
    """Крупнейшие сделки за период — поштучно, из архива блоков.

    Валюту фиксируем рублёвой: у block_trade value номинирован в валюте
    расчётов, и юаневый выпуск с value=800000 иначе встал бы в рейтинг рублей
    как «800 тыс», а долларовый — наоборот, перекрыл бы весь масштаб."""
    if not days:
        return []
    from services.portfolio_db import _connect
    lo, hi = min(days), max(days)
    with _connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT isin, secid, ts, market, price, value, y_idx_bps "
            "FROM block_trade WHERE ts >= ? AND ts <= ? "
            "AND (cur IS NULL OR cur='SUR') "
            "ORDER BY value DESC LIMIT ?",
            # с запасом: дальше выбрасываем дубли и лишние строки одной бумаги
            (f"{lo} 00:00:00", f"{hi} 23:59:59", max(1, BLOCKS_TOP) * 6))]
    placed = _placement_dates() if SKIP_PLACEMENT else {}
    out, seen, per_isin = [], set(), {}
    for r in rows:
        if placed.get(r["isin"]) == (r.get("ts") or "")[:10]:
            continue                       # первичка, не вторичный оборот
        # Лента блоков ходит по всему рынку и класса бумаги не знает: он есть
        # только в нашей свёртке. Бумага, которой в свёртке нет, в альбом
        # класса не идёт — иначе в «Фиксах» всплывали бы флоатерные тикеты.
        if kinds is not None and kinds.get(r["isin"]) is None:
            continue
        # Один пакет приезжает несколькими строками с одинаковой ценой, суммой и
        # секундой (расчёты дробят тикет по счетам). В рейтинге это выглядит
        # как две новости вместо одной.
        key = (r["isin"], r.get("ts"), r.get("value"), r.get("price"))
        if key in seen:
            continue
        seen.add(key)
        # Не больше двух строк на бумагу: в день размещения крупного выпуска
        # его РПС забирали бы весь график, и остальной рынок пропадал.
        n = per_isin.get(r["isin"], 0)
        if n >= BLOCKS_PER_ISIN:
            continue
        per_isin[r["isin"]] = n + 1
        out.append(r)
        if len(out) >= BLOCKS_TOP:
            break
    return out


def _new_issues(days: int) -> List[dict]:
    """Свежие выпуски за окно — сюжет недельного разбора."""
    try:
        from services import instruments_registry as reg
        return reg.list_new_issues(days=days)[:NEW_ISSUES_TOP]
    except Exception as e:
        logger.warning("digest: свежие выпуски недоступны: %s", e)
        return []


def _market_of(r: dict) -> str:
    """Рынок строки: 'fixed' — фикс (g-спред), иначе флоатер (Y-IDX).

    Ключ разделения всего дайджеста: премии двух рынков считаются разными
    метриками, и сводить их в один рейтинг или одну медиану нельзя."""
    return "fixed" if r.get("kind") == "fixed" else "floater"


def _pick_sides(rows: List[dict]) -> List[dict]:
    """Крайние движения рынка: MOVERS_SIDE вверх и столько же вниз."""
    rows = sorted(rows, key=lambda x: x["delta_bps"], reverse=True)
    if len(rows) > MOVERS_SIDE * 2:
        return rows[:MOVERS_SIDE] + rows[-MOVERS_SIDE:]
    return rows


def _spread_of(r: dict) -> Optional[float]:
    """Спред строки дня: у флоатеров Y-IDX, у фиксов g-спред. Смешивать их в
    одном рейтинге можно — обе метрики в базисных пунктах и обе меряют премию
    к безрисковой кривой; подписать сюжет «премия» честнее, чем выкинуть
    половину рынка."""
    v = r.get("y_idx_close_bps")
    return v if v is not None else r.get("g_spread_close_bps")


def _hourly_profile(day: str, scope: str = "floater") -> List[dict]:
    """Профиль торгового дня по часам: оборот и медианная премия.

    Медиану считаем здесь, а не в SQL: в часе перемешаны Y-IDX флоатеров и
    g-спред фиксов, и «средняя температура» по ним поехала бы за одним крупным
    выбросом. Медиана к выбросам глуха."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ts, kind, value, y_idx_bps, g_spread_bps FROM bar_hourly "
            "WHERE ts >= ? AND ts <= ?", (f"{day} 00:00", f"{day} 23:59"))]
    buckets: dict = {}
    for r in rows:
        hh = (r["ts"] or "")[11:13]
        if not hh:
            continue
        m = _market_of(r)
        if m != scope:
            continue
        b = buckets.setdefault(hh, _empty_slot())
        b["v_" + m] += float(r.get("value") or 0)
        # у часа фикса своя колонка спреда, у флоатера своя — берём по рынку,
        # чтобы в медиану не попал g-спред, посчитанный для другой бумаги
        v = r.get("g_spread_bps") if m == "fixed" else r.get("y_idx_bps")
        if v is not None and SANE_MIN_BPS <= v <= SANE_SPREAD_BPS:
            b[m].append(float(v))
    out = [_slot_row(f"{hh}:00", buckets[hh]) for hh in sorted(buckets)
           if buckets[hh]["v_floater"] or buckets[hh]["v_fixed"]]
    return _thin_out(out)


def _daily_profile(days: List[str], scope: str = "floater") -> List[dict]:
    """То же, что почасовой профиль, но по дням окна — для недельного разбора."""
    out = []
    for d in sorted(days):
        slot = _empty_slot()
        for r in _day_rows(d):
            m = _market_of(r)
            if m != scope:
                continue
            slot["v_" + m] += float(r.get("value") or 0)
            v = _spread_of(r)
            if v is not None and SANE_MIN_BPS <= v <= SANE_SPREAD_BPS:
                slot[m].append(float(v))
        if not (slot["v_floater"] or slot["v_fixed"]):
            continue
        dd = date.fromisoformat(d)
        out.append(_slot_row(f"{dd.day:02d}.{dd.month:02d}", slot))
    return _thin_out(out)


# Доля максимума интервала, ниже которой он считается «тонким»: медиана премии
# там собрана с двух случайных сделок вечерки и уводит линию профиля на сотни
# базисных пунктов, хотя рынок стоял. Столбик оборота при этом остаётся — факт
# «в 21:00 торговали на 90 млн» верный, врёт только средний уровень. 5%: при
# дневном пике 4,4 млрд это 220 млн — граница основной сессии.
THIN_SHARE = float(os.getenv("DIGEST_THIN_SHARE", "0.05"))


def _empty_slot() -> dict:
    return {"v_floater": 0.0, "v_fixed": 0.0, "floater": [], "fixed": []}


def _slot_row(label: str, slot: dict) -> dict:
    return {"label": label,
            "v_float": slot["v_floater"], "v_fixed": slot["v_fixed"],
            "y_float": _median(slot["floater"]), "y_fixed": _median(slot["fixed"])}


def _thin_out(rows: List[dict]) -> List[dict]:
    """Снять медиану премии с тонких интервалов, оставив их оборот.

    Порог считаем ПО КАЖДОМУ рынку отдельно: фиксов в вечерке торгуется больше,
    и общий порог гасил бы флоатерную линию там, где она ещё осмысленна."""
    for m, key in (("float", "y_float"), ("fixed", "y_fixed")):
        vk = "v_" + m
        hi = max((float(r.get(vk) or 0) for r in rows), default=0.0)
        for r in rows:
            if hi and float(r.get(vk) or 0) < hi * THIN_SHARE:
                r[key] = None
    return rows


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


# Порядок бакетов — от лучшего к худшему, как их читают на деске. NR
# («без рейтинга») не показываем: в бакете вперемешку свежие выпуски, у
# которых рейтинг ещё не доехал, и экзотика — медиана по такой смеси
# ничего не значит.
_RATING_ORDER = ("AAA", "AA", "A", "BBB", "BB", "B")


def _rating_buckets(isins: List[str]) -> dict:
    """{isin: бакет} для всего дня: кэш рейтингов, а не реестр.

    В реестре рейтинг проставлен только флоатерам — со входом фиксов их слой
    целиком выпадал из этого сюжета. Кэш `ratings` живёт отдельно и покрывает
    рынок: 1167 бумаг из 1239 в типичном дне."""
    try:
        from services import ratings
        return ratings.bucket_map(list(isins))
    except Exception as e:
        logger.warning("digest: рейтинги недоступны: %s", e)
        return {}


def _rating_medians(today: dict, labels: dict, name_of) -> dict:
    """Медиана премии по рейтинговым бакетам, отдельно флоатеры и фиксы.

    Раздельно — потому что метрики разные (Y-IDX против g-спреда): в одном
    столбике они складывались бы в число, которого нет ни у кого."""
    from services.ratings import rating_to_bucket
    grid: dict = {b: {"floater": [], "fixed": []} for b in _RATING_ORDER}
    cached = _rating_buckets(list(today.keys()))
    for isin, r in today.items():
        v = _spread_of(r)
        meta = labels.get(isin) or {}
        # СТУПЕНЬ схлопываем в грейд: в реестре рейтинг лежит и как «AA», и как
        # «AA-» (слой bondresearch даёт ступень), а сетка дайджеста — по грейдам.
        # Без свёртки бумага со ступенью не попадала в grid и молча выпадала.
        bucket = rating_to_bucket(meta.get("rating") or cached.get(isin))
        if v is None or bucket not in grid or not name_of(isin):
            continue
        if not (SANE_MIN_BPS <= v <= SANE_SPREAD_BPS) or (r.get("value") or 0) < MIN_VALUE_RUB:
            continue
        grid[bucket]["fixed" if r.get("kind") == "fixed" else "floater"].append(float(v))
    cats, floaters, fixed = [], [], []
    for b in _RATING_ORDER:
        f, x = grid[b]["floater"], grid[b]["fixed"]
        if not f and not x:
            continue
        cats.append(b)
        floaters.append(_median(f))
        fixed.append(_median(x))
    return {"cats": cats, "floater": floaters, "fixed": fixed}


def _map_points(today: dict, name_of, maturity_of) -> List[dict]:
    """Точки карты «срок × премия»: только то, что сегодня торговалось.

    Срок берём до ПОГАШЕНИЯ из реестра: горизонт прайсинга (оферта) в дневной
    свёртке лежит колонкой horizon, но по нему нет даты — а карта строится по
    оси лет, и смешивать в ней разные базы срока нельзя."""
    out = []
    today_d = date.today()
    for isin, r in today.items():
        v = _spread_of(r)
        name, mat = name_of(isin), maturity_of(isin)
        if v is None or not name or not mat:
            continue
        if not (SANE_MIN_BPS <= v <= SANE_SPREAD_BPS) or (r.get("value") or 0) < MIN_VALUE_RUB:
            continue
        try:
            years = (date.fromisoformat(str(mat)[:10]) - today_d).days / 365.25
        except Exception:
            continue
        if years <= 0:
            continue
        out.append({"x": years, "y": float(v), "v": float(r.get("value") or 0),
                    "kind": r.get("kind"), "name": name})
    return out


async def _warm_moex_meta() -> None:
    """Прогреть суточный справочник ISS перед сборкой.

    Он живёт в памяти процесса и наполняется демоном блоков — но полагаться на
    «кто-то уже сходил» нельзя: в свежем процессе (или если демон выключен)
    карта пуста, и весь слой фиксов молча выпадал бы из альбома по отсутствию
    имени. Один запрос на 3100 бумаг, ошибка не фатальна — просто вернёмся к
    реестру."""
    try:
        from services import block_trades as bt
        if not (bt._secmap.get("map") or {}):
            await asyncio.wait_for(bt.secid_map(), timeout=45)
    except Exception as e:
        logger.warning("digest: справочник MOEX не прогрелся: %s", e)


def _moex_meta() -> dict:
    """{isin: {name, maturity}} из суточного справочника ISS.

    Реестр знает ТОЛЬКО флоатеры, а дневная свёртка давно шире: фиксы (витрина
    ФИКСЫ) в ней есть, но имени у них в реестре нет — и весь их слой молча
    выпадал из движений, оборотов и карты. Справочник блоков покрывает весь
    рынок и уже прогрет демоном; пустая карта (свежий процесс) просто вернёт
    прежнее поведение."""
    try:
        from services import block_trades as bt
        return {v["isin"]: {"name": v.get("name"), "maturity": v.get("maturity")}
                for v in (bt._secmap.get("map") or {}).values() if v.get("isin")}
    except Exception as e:
        logger.warning("digest: справочник MOEX недоступен: %s", e)
        return {}


def _secid_name(b: dict) -> str:
    """Подпись сделки в бумаге, которой нет в реестре: лента блоков ходит по
    всему рынку. У ОФЗ secid читаемее ISIN (SU26252RMFS5 → «ОФЗ 26252»), у
    остальных остаётся тикер."""
    sec = (b.get("secid") or "").strip()
    if sec.startswith("SU") and len(sec) > 7:
        return f"ОФЗ {sec[2:7]}"
    return sec or b["isin"]


def collect(mode: str = "day", scope: str = "floater") -> dict:
    """Всё, что нужно альбому ОДНОГО класса, одним снимком базы.

    mode='week' — то же самое в недельном окне: база сравнения отодвигается на
    WEEK_SESSIONS сессий назад, обороты складываются за все дни окна."""
    week = mode == "week"
    if scope not in SCOPES:
        scope = "floater"
    days = _last_days(max(STREAK_DAYS, WEEK_SESSIONS + 1))
    if not days:
        return {"day": None, "mode": mode}
    day = days[0]
    back = WEEK_SESSIONS if week else 1
    prev = days[back] if len(days) > back else (days[-1] if len(days) > 1 else None)
    today = {r["isin"]: r for r in _day_rows(day) if _market_of(r) == scope}
    yday = ({r["isin"]: r for r in _day_rows(prev) if _market_of(r) == scope}
            if prev else {})

    from services import instruments_registry as reg
    labels = reg.labels_map(list(today.keys())) or {}
    moex = _moex_meta()

    def name_of(isin: str) -> Optional[str]:
        """Имя выпуска: реестр (флоатеры) → справочник MOEX (весь рынок).
        None — бумаги нет нигде: строка с голым ISIN в дайджесте выглядит
        сбоем, а не бумагой, и такие мы пропускаем."""
        return ((labels.get(isin) or {}).get("name")
                or (moex.get(isin) or {}).get("name"))

    def maturity_of(isin: str) -> Optional[str]:
        return ((labels.get(isin) or {}).get("maturity")
                or (moex.get(isin) or {}).get("maturity"))

    hist = _spread_history(days[:STREAK_DAYS])
    movers: List[dict] = []
    for isin, r in today.items():
        cur, was = _spread_of(r), _spread_of(yday.get(isin) or {})
        name = name_of(isin)
        if cur is None or was is None or not name:
            continue
        if (r.get("value") or 0) < MIN_VALUE_RUB:
            continue
        if not (SANE_MIN_BPS <= cur <= SANE_SPREAD_BPS) or abs(cur - was) > SANE_DELTA_BPS:
            continue
        if not (SANE_MIN_BPS <= was <= SANE_SPREAD_BPS):
            continue
        movers.append(
            {"name": name, "isin": isin, "kind": scope,
             "delta_bps": cur - was, "y_bps": cur,
             "streak": _streak(hist.get(isin) or {}, days[:STREAK_DAYS],
                               cur - was)})
    picked = _pick_sides(movers)

    window = days[:back] if week else [day]
    if week:
        # Обороты за неделю складываем по бумаге: вопрос недельного разбора —
        # «где всю неделю были деньги», и один громкий день его не закрывает.
        agg: dict = {}
        for d in window:
            for r in _day_rows(d):
                if not r.get("value") or _market_of(r) != scope:
                    continue
                a = agg.setdefault(r["isin"], {"value": 0.0, "trades": 0,
                                               "kind": r.get("kind")})
                a["value"] += float(r["value"])
                a["trades"] += int(r.get("trades") or 0)
        known = [{"isin": i, "value": a["value"], "trades": a["trades"],
                  "kind": a["kind"]} for i, a in agg.items() if name_of(i)]
    else:
        known = [r for r in today.values() if r.get("value") and name_of(r["isin"])]
    turn = sorted(known, key=lambda r: r["value"], reverse=True)[:TURNOVER_TOP]
    turnover = [{"name": name_of(r["isin"]), "isin": r["isin"], "value": r["value"],
                 "kind": _market_of(r), "trades": r.get("trades")} for r in turn]

    # {isin: класс} по окну — фильтр ленты блоков (см. _blocks)
    kinds = {i: scope for i in today}
    for d in window[1:]:
        for r in _day_rows(d):
            if _market_of(r) == scope:
                kinds.setdefault(r["isin"], scope)
    blocks = []
    for b in _blocks(window, kinds):
        nm = name_of(b["isin"]) or _secid_name(b)
        blocks.append({"name": nm, "isin": b["isin"], "value": b["value"],
                       "price": b.get("price"), "market": b.get("market"),
                       "time": (b.get("ts") or "")[11:16] if not week
                               else (b.get("ts") or "")[5:10],
                       "y_bps": b.get("y_idx_bps")})

    return {"day": day, "prev": prev, "mode": mode, "scope": scope,
            "window": window,
            "movers": picked, "turnover": turnover, "blocks": blocks,
            # широта движения — по ВСЕМ дельтам класса, а не по отобранному топу
            "deltas": [x["delta_bps"] for x in movers],
            "map": _map_points(today, name_of, maturity_of),
            "ratings": _rating_medians(today, labels, name_of),
            "profile": (_daily_profile(window, scope) if week
                        else _hourly_profile(day, scope)),
            "market_value": sum(r["value"] for r in known),
            "traded": len(known),
            "new_issues": _new_issues(7) if week and scope == "floater" else [],
            "curve": (_curve_series(date.fromisoformat(day))
                      if SCOPES[scope]["curve"] else {})}


def _curve_series(day: date) -> dict:
    """Своп-кривая КС сегодня против среза недельной давности.

    KEYRATE, а не RUONIA: флоатер-деск считает премию к ключевой, и именно
    сдвиг этой кривой объясняет, почему у всех разом уехал спред."""
    from services import curve_history
    try:
        now = curve_history.quotes_asof("KEYRATE", day)
        was = curve_history.quotes_asof("KEYRATE", day - timedelta(days=CURVE_LOOKBACK_DAYS),
                                        max_lag_days=CURVE_LOOKBACK_DAYS + 7)
    except Exception as e:
        logger.warning("digest: кривая недоступна: %s", e)
        return {}
    if not now:
        return {}
    cur = {q.tenor: q.value for q in now}
    old = {q.tenor: q.value for q in (was or [])}
    tenors = [t for t in _TENOR_ORDER if t in cur]
    if len(tenors) < 2:
        return {}
    out = {"labels": tenors,
           "today": [cur.get(t) for t in tenors],
           "asof": now[0].date.isoformat() if now else None}
    if old:
        out["prev"] = [old.get(t) for t in tenors]
        out["prev_asof"] = was[0].date.isoformat()
    return out


async def _payment_days(day: date, horizon: int = PAYMENT_DAYS) -> tuple:
    """Купоны и погашения юниверса по дням вперёд. Календарь свой кэш держит
    сам (ключ — торговая дата + версия справочника), так что вечерний вызов
    почти всегда попадает в готовое."""
    try:
        from services import payments_calendar
        cal = await payments_calendar.build_payments_calendar()
    except Exception as e:
        logger.warning("digest: календарь выплат недоступен: %s", e)
        return [], day
    # Окно считаем от ДАТЫ РАСЧЁТА КАЛЕНДАРЯ, а не от дня свёртки: свёртка
    # отстаёт (последний закрытый торговый день), и после выходных окно
    # начиналось бы в прошлом, где будущих платежей уже нет.
    base = cal.get("calc_date")
    if isinstance(base, str):
        try:
            base = date.fromisoformat(base)
        except Exception:
            base = None
    base = base or day
    buckets: dict = {}
    till = base + timedelta(days=horizon)
    for e in cal.get("events") or []:
        d = e.get("date")
        d = date.fromisoformat(d) if isinstance(d, str) else d
        if not d or not (base < d <= till) or e.get("paid"):
            continue
        b = buckets.setdefault(d, {"coupon": 0.0, "redemption": 0.0})
        key = "coupon" if e.get("type") == "COUPON" else "redemption"
        # total_rub — платёж по ВСЕМУ выпуску; amount_rub в тех же событиях
        # это выплата на одну бумагу (48 ₽ купона), и суммировать надо первое:
        # календарь отвечает «сколько денег придёт на рынок», а не «сколько на
        # штуку».
        b[key] += float(e.get("total_rub") or e.get("amount_rub") or 0)
    return ([{"label": f"{d.day:02d}.{d.month:02d}", "date": d,
              "coupon": v["coupon"], "redemption": v["redemption"]}
             for d, v in sorted(buckets.items())], base)


# ── персональная строка: сигналы владельца чата за день ────────────────────
def _signals_line(email: Optional[str], day: str) -> Optional[str]:
    """«Сработало 14 сигналов, чаще всех — X, Y».

    Считаем по СВОИМ фильтрам чата: альбом у всех один (картинки — про рынок),
    а вот сигналы личные, и общая цифра «по всем пользователям» не значила бы
    для читателя ничего."""
    if not email:
        return None
    try:
        from services import signals
        rows = signals.events_for_user(email, limit=500)
    except Exception as e:
        logger.warning("digest: сигналы недоступны: %s", e)
        return None
    from services.signals import event_moment
    hits: dict = {}
    total = 0
    for e in rows:
        if event_moment(e.get("fired_at")).astimezone(_MSK).date().isoformat() != day:
            continue
        total += 1
        nm = e.get("name") or e.get("isin") or "?"
        hits[nm] = hits.get(nm, 0) + 1
    if not total:
        return None
    top = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)[:3]
    tail = ", ".join(f"{html.escape(n)} ×{k}" for n, k in top)
    return f"🔔 Ваших сигналов за день: <b>{total}</b> — {tail}."


# ── сборка альбома ─────────────────────────────────────────────────────────
def _bond_link(name: str, isin: str) -> str:
    """Имя выпуска ссылкой на его график. Имена приходят из внешних справочников
    и содержат «&», «Б1-О» и прочее — без экранирования parse_mode=HTML роняет
    отправку целиком."""
    return f'<a href="{tg_links.bond(isin)}">{html.escape(str(name))}</a>'


def _caption(data: dict, pays: List[dict], email: Optional[str] = None) -> str:
    week = data.get("mode") == "week"
    sc = SCOPES.get(data.get("scope") or "floater") or SCOPES["floater"]
    d = date.fromisoformat(data["day"])
    head = "🗓 <b>Итоги недели" if week else "📊 <b>Разбор дня"
    lines = [f"{head} · {sc['title']} · {_ru_date(d)}</b>"]
    val = data.get("market_value") or 0
    if val:
        span = "за неделю" if week else "за день"
        lines.append(f"Оборот {span} <b>{ch._money(val)} ₽</b> "
                     f"по {data.get('traded', 0)} бумагам.")
    movers = data.get("movers") or []
    base = "к прошлой неделе" if week else "к предыдущему дню"

    def _mover(m: dict, verb: str) -> str:
        # ISIN отдельным <code> — из чата его копируют в поиск дашборда и в
        # заявку; в ссылке он не выделяется одним тапом.
        tail = f" · {m['streak'] + 1}-й день подряд" if (m.get("streak") or 0) >= 2 else ""
        return (f"{verb} {_bond_link(m['name'], m['isin'])} "
                f"{m['delta_bps']:+.0f} бп → {m['y_bps']:.0f}{tail}\n"
                f"<code>{html.escape(m['isin'])}</code>")

    if movers:
        wide, tight = movers[0], movers[-1]
        if wide["delta_bps"] > 0:
            lines.append(_mover(wide, "Шире всех"))
        if tight["delta_bps"] < 0:
            lines.append(_mover(tight, "Сильнее всех сжался"))
        lines.append(f"<i>Движения {base}, премия — {sc['metric']}.</i>")
    blocks = data.get("blocks") or []
    if blocks:
        b = blocks[0]
        kind = "адресная" if (b.get("market") == "ndm") else "безадресная"
        lines.append(f"Крупнейший тикет — {_bond_link(b['name'], b['isin'])} "
                     f"на <b>{ch._money(float(b['value']))} ₽</b> ({kind}).")
    sig = _signals_line(email, data["day"])
    if sig:
        lines.append(sig)
    news = data.get("new_issues") or []
    if news:
        names = ", ".join(html.escape(str(n.get("short_name") or n["isin"]))
                          for n in news)
        lines.append(f"🆕 Свежие выпуски недели: {names}.")
    week_sum = sum(p["coupon"] + p["redemption"] for p in pays)
    if week_sum:
        horizon = PAYMENT_DAYS_WEEK if week else PAYMENT_DAYS
        lines.append(f"В ближайшие {horizon} дней придёт "
                     f"<b>{ch._money(week_sum)} ₽</b> выплат.")
    return "\n".join(lines)


def _buttons() -> dict:
    """Клавиатура следом за альбомом. Только url-кнопки: вебхук подписан на
    ["message"], callback_query никто не разбирает — нажатие на callback-кнопку
    висело бы часиками навсегда."""
    return {"inline_keyboard": [
        [{"text": "📈 Движения", "url": tg_links.page("floaters")},
         {"text": "🔔 Сигналы", "url": tg_links.page("signals")}],
        [{"text": "💼 Крупные сделки", "url": tg_links.page("blocks")},
         {"text": "💰 Выплаты", "url": tg_links.page("payments")}],
    ]}


async def build_album(mode: str = "day", scope: str = "floater") -> tuple:
    """→ (items для sendMediaGroup, подпись, контекст). Пустой список — рисовать
    нечего (нерабочий день, пустая база): воркер тогда шлёт короткую строку, а
    не пачку заглушек.

    Один альбом = один класс рынка (см. SCOPES). Контекст возвращаем, чтобы
    подпись пересобиралась под каждый чат (в ней личные сигналы), не
    перерисовывая картинки."""
    await _warm_moex_meta()
    data = await asyncio.to_thread(collect, mode, scope)
    if not data.get("day"):
        return [], "", {}
    sc = SCOPES.get(scope) or SCOPES["floater"]
    week = mode == "week"
    d = date.fromisoformat(data["day"])
    horizon = PAYMENT_DAYS_WEEK if week else PAYMENT_DAYS
    # Календарь выплат построен по универсу флоатеров: у фикс-альбома этого
    # сюжета просто нет — рисовать пустые столбики честнее не показывать вовсе.
    pays, pay_from = await _payment_days(d, horizon) if sc["payments"] else ([], d)
    sub = _ru_date(d)
    prev_iso = data.get("prev")
    # Тире, а не стрелка: в подписи КАРТИНКИ работает шрифт контейнера, и на
    # системном Arial (дев на маке) «→» рисуется квадратом-тофу.
    span = (f"{_ru_date(date.fromisoformat(prev_iso))} — {sub}"
            if week and prev_iso else sub)
    head = f"{sc['title']} · "

    def _render() -> list:
        out = []
        out.append(("movers.png", ch.movers(
            data.get("movers") or [],
            f"{head}движения премии за {'неделю' if week else 'день'}",
            f"{span} · Δ бп {sc['metric']} к "
            f"{'прошлой неделе' if week else 'предыдущему дню'}, "
            f"оборот от {ch._money(MIN_VALUE_RUB)} {ch.rub()}"), None))
        out.append(("breadth.png", ch.breadth(
            data.get("deltas") or [],
            f"{head}широта движения",
            f"{span} · сколько бумаг разъехалось и насколько"), None))
        out.append(("turnover.png", ch.turnover(
            data.get("turnover") or [],
            f"{head}обороты {'недели' if week else 'дня'}", span), None))
        out.append(("blocks.png", ch.blocks(
            data.get("blocks") or [],
            f"{head}крупные сделки {'недели' if week else 'дня'}", span), None))
        out.append(("map.png", ch.scatter(
            data.get("map") or [], f"{head}карта рынка",
            f"{sub} · размер точки — оборот дня, оборот от "
            f"{ch._money(MIN_VALUE_RUB)} {ch.rub()}",
            y_label=f"премия ({sc['metric']}), бп", legend=False), None))
        rt = data.get("ratings") or {}
        out.append(("ratings.png", ch.grouped(
            rt.get("cats") or [],
            [{"label": f"медиана {sc['metric']}",
              "color": ch.ACCENT if scope == "floater" else ch.ACCENT2,
              "values": rt.get(scope) or []}],
            f"{head}премия по рейтингам", f"{sub} · медиана, бп"), None))
        out.append(("profile.png", ch.profile(
            data.get("profile") or [],
            f"{head}профиль {'недели' if week else 'дня'}",
            f"{span} · оборот по {'дням' if week else 'часам'} и медиана премии",
            bar_label="оборот по дням" if week else "оборот по часам"), None))
        cur = data.get("curve") or {}
        if cur:
            series = []
            if cur.get("prev"):
                series.append({"label": _label_date(cur.get("prev_asof"), "неделю назад"),
                               "values": cur["prev"], "color": ch.MUTED})
            if cur.get("today"):
                series.append({"label": _label_date(cur.get("asof"), sub),
                               "values": cur["today"], "color": ch.ACCENT})
            out.append(("curve.png", ch.curve(
                series, cur.get("labels") or [], "Своп-кривая КС",
                f"{sub} · par-ставки, %"), None))
        if pays:
            # Подзаголовок — от даты КАЛЕНДАРЯ: свёртка дня может отставать, и
            # «14 августа · ближайшие 10 дней» над столбиками с 27.08 сбивало бы.
            out.append(("payments.png", ch.payments(
                pays, "Выплаты вперёд",
                f"с {_ru_date(pay_from)} · {horizon} дней"), None))
        return out

    # Рендер — чистый CPU на семь-девять картинок: в потоке, чтобы не держать цикл
    items = await asyncio.to_thread(_render)
    caption = _caption(data, pays)
    items[0] = (items[0][0], items[0][1], caption)
    return items, caption, {"data": data, "pays": pays}


def _recipients() -> List[dict]:
    """Чаты, которым идёт дайджест: одобренные и не заглушённые. Отдельного
    согласия не спрашиваем — это та же подписка, что и на сигналы, только раз
    в день (выключается тем же /mute). Отдаём строки целиком: подпись личная,
    и адресату нужен ещё email владельца."""
    return [r for r in tg_users.list_all()
            if r.get("status") == "approved" and r.get("chat_id")
            and not r.get("muted")]


async def send_digest(chat_ids: Optional[List[int]] = None,
                      mode: str = "day",
                      scopes: Optional[List[str]] = None) -> int:
    """Разослать альбомы классов подряд. → сколько чатов получило хоть один.

    Альбомы собираются ОДИН раз на всех, а подпись пересобирается под каждый
    чат: картинки общие (это рынок), личная в них только строка сигналов."""
    if not telegram.enabled():
        return 0
    if not ch.fonts_ok():
        logger.warning("digest: нет TTF-шрифта — альбом не шлём "
                       "(нужен пакет fonts-dejavu-core)")
        return 0
    order = [x for x in (scopes or DEFAULT_SCOPES) if x in SCOPES] or ["floater"]
    albums = []
    for scope in order:
        items, _cap, ctx = await build_album(mode, scope)
        if items:
            albums.append((scope, items, ctx))
        else:
            logger.info("digest: данных за день нет (%s) — пропускаю", scope)
    if not albums:
        if chat_ids:
            # Ручной вызов: молчание в ответ на команду читается как поломка
            for chat_id in chat_ids:
                await telegram.send_message(
                    chat_id, "Свёртка дня ещё пуста — торгов не было "
                             "или день не закрыт.")
        return 0
    if chat_ids is not None:
        targets = [{"chat_id": c, "email": tg_users.email_for_chat(c)}
                   for c in chat_ids]
    else:
        targets = _recipients()
    sent = 0
    for t in targets:
        chat_id = int(t["chat_id"])
        got = False
        for _scope, items, ctx in albums:
            try:
                personal = _caption(ctx["data"], ctx["pays"], t.get("email"))
                body = [(items[0][0], items[0][1], personal)] + list(items[1:])
                if await telegram.send_media_group(chat_id, body):
                    got = True
            except Exception as e:
                logger.warning("digest send error (chat %s): %s", chat_id, e)
        if got:
            sent += 1
            # Кнопки — одним сообщением в конце и без звука: альбомы уже
            # прозвенели, а клавиатура у каждого была бы третьей копией одних
            # и тех же ссылок.
            try:
                await telegram.send_message(
                    chat_id, "Открыть на дашборде:",
                    reply_markup=_buttons(), disable_notification=True)
            except Exception as e:
                logger.warning("digest buttons error (chat %s): %s", chat_id, e)
    logger.info("digest: %d альбом(ов) в %d чат(ов)", len(albums), sent)
    return sent


# ── воркер ─────────────────────────────────────────────────────────────────
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "1") not in ("0", "false", "no")
DIGEST_AT = os.getenv("DIGEST_AT", "19:30")      # МСК
DIGEST_WEEKLY = os.getenv("DIGEST_WEEKLY", "1") not in ("0", "false", "no")
# День недели недельного разбора (0 = понедельник). Пятница: неделя закрыта,
# а разбор ещё успевает попасть в рабочий вечер.
DIGEST_WEEKLY_DAY = int(os.getenv("DIGEST_WEEKLY_DAY", "4"))


async def digest_worker() -> None:
    """Раз в сутки после закрытия основной сессии.

    19:30 МСК по умолчанию: вечерка ещё идёт, но дневной оборот и закрытие уже
    сложились, а часовой демон успел свернуть день (:07 каждого часа). Выходные
    пропускаем по календарю самой базы: если торгов не было, свежего дня в
    bar_daily просто нет и build_album вернёт пусто.

    В пятницу вместо дневного уходит недельный разбор — дневной и недельный в
    один вечер читать никто не станет. Классы (флоатеры, фиксы) идут одним
    прогоном друг за другом: см. DIGEST_SCOPES."""
    if not DIGEST_ENABLED:
        logger.info("дайджест выключен (DIGEST_ENABLED=0)")
        return
    try:
        hh, mm = (int(x) for x in DIGEST_AT.split(":"))
    except Exception:
        hh, mm = 19, 30
    sent_on: Optional[date] = None
    while True:
        now = datetime.now(_MSK)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        today = datetime.now(_MSK).date()
        # Будни: в субботу торгов нет, а дайджест за пятницу уже ушёл
        if today.weekday() >= 5 or sent_on == today:
            continue
        try:
            mode = "week" if (DIGEST_WEEKLY and today.weekday() == DIGEST_WEEKLY_DAY) \
                else "day"
            await send_digest(mode=mode)
            sent_on = today
        except Exception as e:
            logger.warning("digest worker error: %s", e)
