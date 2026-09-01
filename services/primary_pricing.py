"""Спред анонсов первички ПО НАШЕЙ МОДЕЛИ: ориентир организатора → метрика,
сравнимая со вторичкой один-в-один.

Смысл витрины: организатор объявляет «КС + не выше 300 бп», а монитор в этот же
момент показывает, за сколько торгуются сопоставимые бумаги. Пока ориентир
лежит текстом, сравнить его с рынком глазами нельзя — разная частота купона,
разный срок, разная форма кривой. Здесь текст разбирается в параметры, а
считает их services/custom_bond — ТОТ ЖЕ путь, что вкладка КАЛЬКУЛЯТОР и
строки таблиц. Никакой отдельной математики в этом модуле нет.

ЧТО ИМЕННО СЧИТАЕТСЯ. Бумага берётся по номиналу (цена 100 — размещение),
погашение = дата размещения + срок из выгрузки. Срок в источнике — «обращения /
ДО ОФЕРТЫ», то есть уже горизонт прайсинга (см. pricing-horizon-rule): для
выпуска с офертой считается к оферте, как и положено.

ГРАНИЦА, А НЕ ОЦЕНКА. Почти все ориентиры — потолок («не выше 300 бп»), и
книга почти всегда закрывается НИЖЕ. Спред при потолочном купоне — верхняя
граница: реальная бумага выйдет с этим спредом или уже. Поэтому расчёт несёт
bound (exact/max/range), а витрина обязана рисовать «≤», а не голое число:
голое читалось бы как прогноз, которым оно не является.
"""
from __future__ import annotations

import asyncio
import logging
import re

from services.custom_bond import CustomBondError, price_fixed, price_floater

logger = logging.getLogger(__name__)

# «ежемесячный» → 12 выплат в год. Слова берутся из колонки type выгрузки.
FREQ_WORDS = {
    "ежемесячный": 12, "ежемесячно": 12, "месячный": 12,
    "ежеквартальный": 4, "квартальный": 4, "ежеквартально": 4,
    "полугодовой": 2, "раз в полгода": 2, "полугодовай": 2,
    "ежегодный": 1, "годовой": 1, "раз в год": 1,
}

# База плавающего купона в тексте ориентира. КС = ключевая ставка.
_BASES = [
    (re.compile(r"\bкс\b|ключев", re.I), "KEYRATE"),
    (re.compile(r"ruonia|руони", re.I), "RUONIA"),
]

_NUM = r"\d+(?:[.,]\d+)?"
# «не выше 300 бп» / «+ 150 бп» — маржа флоатера в БАЗИСНЫХ ПУНКТАХ
_RE_BPS = re.compile(rf"\+?\s*(?:(?P<cap>не\s+выше|до)\s*)?(?P<v>{_NUM})\s*(?:б\.?\s?п|бп|bp)", re.I)
# «не выше 17,5%» / «24%» / «24,00 - 25,50%» — ставка фикса в ПРОЦЕНТАХ
_RE_RANGE_PCT = re.compile(rf"(?P<lo>{_NUM})\s*[-–—]\s*(?P<hi>{_NUM})\s*%", re.I)
_RE_PCT = re.compile(rf"(?:(?P<cap>не\s+выше|до)\s*)?(?P<v>{_NUM})\s*%", re.I)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def parse_freq(word: str | None) -> int | None:
    w = (word or "").strip().lower()
    return FREQ_WORDS.get(w)


def parse_coupon_guide(text: str | None) -> dict | None:
    """Текст ориентира → параметры купона. None, если ставки в тексте нет
    («будет определен позднее» — такие строки просто остаются без спреда).

    bound: exact — названа точная ставка; max — потолок («не выше»);
    range — вилка «24,00 - 25,50%» (обе границы считаются).
    """
    t = (text or "").strip()
    if not t:
        return None

    base = next((b for rx, b in _BASES if rx.search(t)), None)
    if base:
        # флоатер: маржа к базе. Проценты в тексте флоатера не встречаются,
        # но если организатор напишет «КС + 3%» — тоже маржа, ×100 в бп.
        m = _RE_BPS.search(t)
        if m:
            v = _f(m.group("v"))
            return {"kind": "floater", "base": base, "margin_bps": v,
                    "bound": "max" if m.group("cap") else "exact"}
        m = _RE_PCT.search(t)
        if m:
            return {"kind": "floater", "base": base, "margin_bps": _f(m.group("v")) * 100,
                    "bound": "max" if m.group("cap") else "exact"}
        return None

    # фикс: сначала вилка, иначе одиночная ставка
    m = _RE_RANGE_PCT.search(t)
    if m:
        lo, hi = _f(m.group("lo")), _f(m.group("hi"))
        return {"kind": "fixed", "rate_pct": max(lo, hi), "rate_pct_low": min(lo, hi),
                "bound": "range"}
    m = _RE_PCT.search(t)
    if m:
        return {"kind": "fixed", "rate_pct": _f(m.group("v")),
                "bound": "max" if m.group("cap") else "exact"}
    return None


def _add_months(d, months: int):
    from datetime import date
    y, mo = divmod(d.year * 12 + (d.month - 1) + months, 12)
    mo += 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, mo, day)
        except ValueError:
            continue
    return date(y, mo, 28)


def maturity_for(row: dict):
    """Дата погашения анонса: размещение + срок. Срок в выгрузке — «обращения /
    до оферты», то есть горизонт прайсинга уже учтён источником. Без даты
    размещения (книга ещё не назначена) горизонта нет — и спреда тоже."""
    from datetime import date
    start = row.get("issue_date") or row.get("book_date")
    if not start or not row.get("term_years"):
        return None
    try:
        d = date.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    months = int(round(float(row["term_years"]) * 12))
    return _add_months(d, months) if months > 0 else None


async def _spread_one(spec: dict, freq: int, maturity, rate_pct: float | None = None):
    """Один прогон движка при цене 100. Возвращает (спред в бп, дюрация)."""
    if spec["kind"] == "floater":
        r = await price_floater(spec["base"], spec["margin_bps"], freq, maturity, 100.0)
        m = r["metrics"]
        return m.get("y_idx_bps"), m.get("spread_dur_yrs")
    r = await price_fixed(rate_pct if rate_pct is not None else spec["rate_pct"],
                          freq, maturity, 100.0)
    m = r["metrics"]
    return m.get("g_spread_bps"), m.get("mod_dur")


async def price_row(row: dict) -> dict | None:
    """Спред одной строки анонса. None — если считать не из чего (нет ставки,
    нет даты размещения, нераспознанная частота).

    Метрика РАЗНАЯ по классам и это осознанно: у флоатера Y-IDX, у фикса
    G-спред — ровно то, что стоит в соответствующей колонке монитора. Класс
    бумаги в строке подписан, так что колонка читается однозначно.
    """
    spec = parse_coupon_guide(row.get("coupon_guide"))
    if not spec:
        return None
    # класс из ориентира должен биться с флагом источника; расходится — верим
    # ТЕКСТУ ориентира (там написана сама формула), но помечаем строку
    mismatch = spec["kind"] == "floater" and not row.get("is_floater")
    freq = parse_freq(row.get("coupon_freq"))
    maturity = maturity_for(row)
    if not freq or not maturity:
        return None

    try:
        bps, dur = await _spread_one(spec, freq, maturity)
        low = None
        if spec["bound"] == "range":
            low, _ = await _spread_one(spec, freq, maturity, spec.get("rate_pct_low"))
    except CustomBondError as e:
        logger.info("первичка %s: спред не посчитан (%s)", row.get("issuer"), e.message)
        return None
    except Exception as e:                                       # noqa: BLE001
        logger.warning("первичка %s: сбой расчёта: %s", row.get("issuer"), e)
        return None

    if bps is None:
        return None
    return {
        "kind": spec["kind"],
        "metric": "y_idx" if spec["kind"] == "floater" else "g_spread",
        "bound": spec["bound"],
        "spread_bps": round(bps),
        "spread_bps_low": round(low) if low is not None else None,
        "dur_yrs": round(dur, 2) if dur is not None else None,
        "maturity": maturity.isoformat(),
        "freq": freq,
        "base": spec.get("base"),
        "margin_bps": spec.get("margin_bps"),
        "rate_pct": spec.get("rate_pct"),
        "class_mismatch": mismatch or None,
    }


async def price_rows(rows: list[dict]) -> list[dict | None]:
    """Спреды всей выгрузки — СПИСОК РАСЧЁТОВ в порядке строк, а не сами строки.

    Модуль намеренно не собирает витрину: строки живут своей жизнью (метка
    «новое» пересчитывается на каждый запрос от сегодняшней даты), и если
    склеить их здесь, мемо ниже заморозило бы вчерашнюю метку вместе с
    расчётом. Склейка — на вызывающей стороне."""
    results = await asyncio.gather(*(price_row(r) for r in rows),
                                   return_exceptions=True)
    out = []
    for r, res in zip(rows, results):
        if isinstance(res, Exception):
            logger.warning("первичка %s: %s", r.get("issuer"), res)
            res = None
        out.append(res)
    return out


_cache: dict = {"key": None, "models": None}
_lock = asyncio.Lock()


def _key(rows: list[dict]) -> tuple:
    """Версия результата = день расчёта + ОТПЕЧАТОК КРИВЫХ ПО СОДЕРЖИМОМУ +
    состав выгрузки. Не curves_ts: он меняется на каждой пересборке кривых, и
    кэш сбрасывался бы раз в 15 минут на неизменившихся котировках (см.
    market_data.curves_fingerprint — там та же грабля описана по факту прода)."""
    import hashlib
    from datetime import date
    from services.market_data import curves_fingerprint, market_cache
    src = repr([(r.get("issuer"), r.get("coupon_guide"), r.get("coupon_freq"),
                 r.get("issue_date"), r.get("book_date"), r.get("term_years"))
                for r in rows])
    return (str(market_cache.get("calc_date") or date.today()),
            curves_fingerprint(market_cache),
            hashlib.sha1(src.encode()).hexdigest()[:16])


async def price_rows_cached(rows: list[dict]) -> list[dict | None]:
    """То же, но с мемо: полный прогон ~80 мс чистого CPU на event loop, а
    выгрузка меняется раз в сутки и кривые — раз в котировку. Кэшируются ТОЛЬКО
    расчёты — см. price_rows о том, почему не строки."""
    key = _key(rows)
    if _cache["key"] == key and _cache["models"] is not None:
        return _cache["models"]
    async with _lock:
        if _cache["key"] == key and _cache["models"] is not None:
            return _cache["models"]
        out = await price_rows(rows)
        _cache["key"], _cache["models"] = key, out
        return out
