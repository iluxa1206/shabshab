"""«Разбор дня» — альбом из четырёх картинок в Telegram после закрытия рынка.

Сигналы говорят про мгновение («вот сейчас кто-то встал широко»), и за день их
набегает столько, что общая картина в ленте не собирается. Дайджест отвечает на
другие вопросы: куда за сегодня уехали спреды, где был оборот, что сделала
кривая и сколько денег придёт на неделе.

Один альбом, а не четыре сообщения: в чате это одна карточка, которую листают,
и она не разрывает ленту сигналов на четыре части.

Данные берём ИЗ УЖЕ ПОСЧИТАННОГО (bar_daily, архив своп-котировок, календарь
выплат): дайджест — витрина, а не ещё один расчётный слой, и падать он не
имеет права даже если какой-то сюжет пуст (картинка тогда честно скажет, что
данных нет)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from services import charts_png as ch
from services import telegram, tg_users

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

# Порог оборота бумаги, ниже которого её движение спреда — не новость, а шум
# одной случайной сделки на пару лотов.
MIN_VALUE_RUB = float(os.getenv("DIGEST_MIN_VALUE", "3000000"))
MOVERS_SIDE = int(os.getenv("DIGEST_MOVERS", "6"))    # столько вверх и столько вниз
TURNOVER_TOP = int(os.getenv("DIGEST_TURNOVER_TOP", "12"))
PAYMENT_DAYS = int(os.getenv("DIGEST_PAYMENT_DAYS", "10"))
CURVE_LOOKBACK_DAYS = int(os.getenv("DIGEST_CURVE_BACK", "7"))
# Санитарные границы. В дневной свёртке попадаются бумаги с битым номиналом или
# экзотикой, у которых «премия» выходит в тысячи процентов; в рейтинге движений
# такая строка занимает весь масштаб и прячет настоящие движения рынка.
SANE_SPREAD_BPS = float(os.getenv("DIGEST_SANE_SPREAD", "3000"))
SANE_DELTA_BPS = float(os.getenv("DIGEST_SANE_DELTA", "1000"))

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
def _last_two_days() -> tuple:
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute("SELECT DISTINCT date FROM bar_daily "
                         "ORDER BY date DESC LIMIT 2").fetchall()
    days = [r["date"] for r in rows]
    return (days[0] if days else None, days[1] if len(days) > 1 else None)


def _day_rows(day: str) -> List[dict]:
    from services.portfolio_db import _connect
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT isin, kind, y_idx_close_bps, g_spread_close_bps, close_pct, "
            "value, trades FROM bar_daily WHERE date=?", (day,))]


def _spread_of(r: dict) -> Optional[float]:
    """Спред строки дня: у флоатеров Y-IDX, у фиксов g-спред. Смешивать их в
    одном рейтинге можно — обе метрики в базисных пунктах и обе меряют премию
    к безрисковой кривой; подписать сюжет «премия» честнее, чем выкинуть
    половину рынка."""
    v = r.get("y_idx_close_bps")
    return v if v is not None else r.get("g_spread_close_bps")


def collect() -> dict:
    """Всё, что нужно альбому, одним снимком базы."""
    day, prev = _last_two_days()
    if not day:
        return {"day": None}
    today = {r["isin"]: r for r in _day_rows(day)}
    yday = {r["isin"]: r for r in _day_rows(prev)} if prev else {}

    from services import instruments_registry as reg
    labels = reg.labels_map(list(today.keys())) or {}

    def name_of(isin: str) -> Optional[str]:
        """None — бумаги нет в реестре. Свёртка bar_daily шире юниверса (в ней
        оседает всё, что попало в стрим), но дайджест — про наш деск, и строка
        с голым ISIN вместо имени в нём выглядит сбоем, а не бумагой."""
        return (labels.get(isin) or {}).get("name")

    movers = []
    for isin, r in today.items():
        cur, was = _spread_of(r), _spread_of(yday.get(isin) or {})
        name = name_of(isin)
        if cur is None or was is None or not name:
            continue
        if (r.get("value") or 0) < MIN_VALUE_RUB:
            continue
        if abs(cur) > SANE_SPREAD_BPS or abs(cur - was) > SANE_DELTA_BPS:
            continue
        movers.append({"name": name, "isin": isin,
                       "delta_bps": cur - was, "y_bps": cur})
    movers.sort(key=lambda x: x["delta_bps"], reverse=True)
    picked = movers[:MOVERS_SIDE] + movers[-MOVERS_SIDE:] if len(movers) > MOVERS_SIDE * 2 \
        else movers

    known = [r for r in today.values() if r.get("value") and name_of(r["isin"])]
    turn = sorted(known, key=lambda r: r["value"], reverse=True)[:TURNOVER_TOP]
    turnover = [{"name": name_of(r["isin"]), "value": r["value"],
                 "trades": r.get("trades")} for r in turn]

    return {"day": day, "prev": prev,
            "movers": picked, "turnover": turnover,
            "market_value": sum(r["value"] for r in known),
            "traded": len(known),
            "curve": _curve_series(date.fromisoformat(day))}


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


async def _payment_days(day: date) -> tuple:
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
    till = base + timedelta(days=PAYMENT_DAYS)
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


# ── сборка альбома ─────────────────────────────────────────────────────────
def _caption(data: dict, pays: List[dict]) -> str:
    d = date.fromisoformat(data["day"])
    lines = [f"📊 <b>Разбор дня · {_ru_date(d)}</b>"]
    val = data.get("market_value") or 0
    if val:
        lines.append(f"Оборот юниверса <b>{ch._money(val)} ₽</b> "
                     f"по {data.get('traded', 0)} бумагам.")
    movers = data.get("movers") or []
    if movers:
        wide, tight = movers[0], movers[-1]
        if wide["delta_bps"] > 0:
            lines.append(f"Шире всех <code>{wide['name']}</code> "
                         f"{wide['delta_bps']:+.0f} бп → {wide['y_bps']:.0f}.")
        if tight["delta_bps"] < 0:
            lines.append(f"Сильнее всех сжался <code>{tight['name']}</code> "
                         f"{tight['delta_bps']:+.0f} бп → {tight['y_bps']:.0f}.")
    week = sum(p["coupon"] + p["redemption"] for p in pays)
    if week:
        lines.append(f"В ближайшие {PAYMENT_DAYS} дней придёт "
                     f"<b>{ch._money(week)} ₽</b> выплат.")
    return "\n".join(lines)


async def build_album() -> tuple:
    """→ (items для sendMediaGroup, подпись). Пустой список — рисовать нечего
    (нерабочий день, пустая база): воркер тогда молчит, а не шлёт четыре
    заглушки."""
    data = await asyncio.to_thread(collect)
    if not data.get("day"):
        return [], ""
    d = date.fromisoformat(data["day"])
    pays, pay_from = await _payment_days(d)
    sub = _ru_date(d)

    def _render() -> list:
        out = []
        out.append(("movers.png", ch.movers(
            data.get("movers") or [], "Движения премии за день",
            f"{sub} · Δ бп к предыдущему дню, оборот от "
            f"{ch._money(MIN_VALUE_RUB)} ₽"), None))
        out.append(("turnover.png", ch.turnover(
            data.get("turnover") or [], "Обороты дня", sub), None))
        cur = data.get("curve") or {}
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
        # Подзаголовок — от даты КАЛЕНДАРЯ: свёртка дня может отставать, и
        # «14 августа · ближайшие 10 дней» над столбиками с 27.08 сбивало бы.
        out.append(("payments.png", ch.payments(
            pays, "Выплаты вперёд",
            f"с {_ru_date(pay_from)} · {PAYMENT_DAYS} дней"), None))
        return out

    # Рендер — чистый CPU на четыре картинки: в потоке, чтобы не держать цикл
    items = await asyncio.to_thread(_render)
    caption = _caption(data, pays)
    items[0] = (items[0][0], items[0][1], caption)
    return items, caption


def _recipients() -> List[int]:
    """Чаты, которым идёт дайджест: одобренные и не заглушённые. Отдельного
    согласия не спрашиваем — это та же подписка, что и на сигналы, только раз
    в день (выключается тем же /mute)."""
    return [int(r["chat_id"]) for r in tg_users.list_all()
            if r.get("status") == "approved" and r.get("chat_id")
            and not r.get("muted")]


async def send_digest(chat_ids: Optional[List[int]] = None) -> int:
    """Разослать альбом. → сколько чатов получило."""
    if not telegram.enabled():
        return 0
    if not ch.fonts_ok():
        logger.warning("digest: нет TTF-шрифта — альбом не шлём "
                       "(нужен пакет fonts-dejavu-core)")
        return 0
    items, _cap = await build_album()
    if not items:
        logger.info("digest: данных за день нет — пропускаю")
        return 0
    targets = chat_ids if chat_ids is not None else _recipients()
    sent = 0
    for chat_id in targets:
        try:
            if await telegram.send_media_group(chat_id, items):
                sent += 1
        except Exception as e:
            logger.warning("digest send error (chat %s): %s", chat_id, e)
    logger.info("digest: отправлен в %d чат(ов)", sent)
    return sent


# ── воркер ─────────────────────────────────────────────────────────────────
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "1") not in ("0", "false", "no")
DIGEST_AT = os.getenv("DIGEST_AT", "19:30")      # МСК


async def digest_worker() -> None:
    """Раз в сутки после закрытия основной сессии.

    19:30 МСК по умолчанию: вечерка ещё идёт, но дневной оборот и закрытие уже
    сложились, а часовой демон успел свернуть день (:07 каждого часа). Выходные
    пропускаем по календарю самой базы: если торгов не было, свежего дня в
    bar_daily просто нет и build_album вернёт пусто."""
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
            await send_digest()
            sent_on = today
        except Exception as e:
            logger.warning("digest worker error: %s", e)
