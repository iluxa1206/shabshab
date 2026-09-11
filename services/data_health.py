"""Сторож ЗДОРОВЬЯ ДАННЫХ: замечать поломку раньше пользователя.

Авария 10.09.2026 прошла незамеченной ровно потому, что снаружи всё выглядело
нормально. iss.moex.com отвалился в 06:22, утренний прогрев в 09:00 молча не
состоялся, расчёт поехал на НКД из слепка от 28.07, и Y-IDX по 41 бумаге
удвоился — а первым это заметил человек, глазами, в середине дня. Ни одного
предупреждения по дороге не было: каждый отдельный слой считал, что у него всё
в порядке (число «не None» — значит источник есть; кэш есть — значит данные
есть; движок крутится — значит живой).

Отсюда четыре независимых вопроса, на каждый свой ответ:

  1. ИСТОЧНИКИ ДАЮТ ДАННЫЕ? — не «порт отвечает», а «данные приходят».
     Предохранитель ISS знает это точнее любой пробы: он размыкается по
     фактическим отказам рабочих запросов, а не по контрольному пингу.
  2. ДАННЫЕ СВЕЖИЕ? — возраст того, на чём считаем. Тихо протухший кэш
     опаснее явного отказа: отказ виден, а протухший кэш отвечает бодро.
  3. ЧИСЛА ПРАВДОПОДОБНЫ? — медиана спреда по рынку день-к-дню. Ловит ЛЮБУЮ
     поломку расчёта, включая ту, которой ещё нет в списке известных.
  4. ДВИЖОК СПРАВЛЯЕТСЯ? — темп пересчёта и длина очереди.

Каждая функция возвращает {ключ: человеческий текст}. Пустой словарь — всё
хорошо. Ключ стабилен между вызовами: по нему сторож отличает новую беду от
уже сообщённой и даёт отбой (см. api.main._watch_alert).
"""
import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

# --- пороги ---------------------------------------------------------------

# Котировки ставок приходят за ПРОШЛЫЙ день, плюс выходные и праздники: до
# четырёх календарных дней отставания законно. Больше — Cbonds не отдаёт.
RATES_STALE_DAYS = 4
# Расписания купонов перевариваются утренним прогревом (09:00 МСК). Полтора
# суток без обновления = прогрев не состоялся ни разу.
SCHEDULE_STALE_HOURS = 36
# Прогрев не отметился за столько часов — он не идёт (а не «ещё не время»).
PREWARM_STALE_HOURS = 30
# Сдвиг медианы Y-IDX по рынку, который НЕ объясняется движением цен.
# Рынок целиком на столько за сутки не ходит: 10.09 сдвиг был ~+116 bps.
SANITY_YIDX_JUMP_BPS = 40.0
# ...и только если цены при этом стояли: медиана |Δцены| ниже этого.
SANITY_PRICE_QUIET_PCT = 0.15
# Меньше этого числа бумаг в пересечении — сравнивать нечего, молчим.
SANITY_MIN_BONDS = 30
# Темп движка в торговые часы. Ниже — конвейер голодает (10.09: 8 строк/мин
# против обычных 450).
ENGINE_MIN_ROWS_PER_MIN = 20
# Очередь пересчёта. Сама по себе длина ни о чём не говорит: после рестарта
# движок пересобирает контексты и сетки, и очередь законно доходит до тысячи,
# а потом тает (11.09: 992 → 814 → 640 → 608 за четыре такта). Тревожно не
# «длинно», а «длинно И НЕ УМЕНЬШАЕТСЯ» — поэтому порог высокий, а решение
# принимается по ТРЕНДУ (см. engine_problems).
ENGINE_MAX_DIRTY = 900
# Сколько проверок подряд очередь должна не уменьшаться, чтобы это считалось
# завалом. Две — это ~20 минут при такте сторожа в 10 минут.
ENGINE_DIRTY_STUCK_CHECKS = 2
# Доля бумаг без биржевого НКД, выше которой считаем это аварией источника,
# а не законными единичными пробелами.
ACCRUED_MISSING_SHARE = 0.5


def _msk_now() -> datetime:
    return datetime.now(_MSK)


def trading_hours(now: Optional[datetime] = None) -> bool:
    """Идёт ли основная сессия. Вне её тишина движка — норма, а не беда."""
    now = now or _msk_now()
    if now.weekday() >= 5:
        return False
    return 10 <= now.hour < 19


def _age_hours(path: str) -> Optional[float]:
    try:
        return (time.time() - os.stat(path).st_mtime) / 3600.0
    except OSError:
        return None


# --- 1. источники ---------------------------------------------------------

def source_problems() -> Dict[str, str]:
    """Кто из источников перестал давать данные.

    Спрашиваем не сеть, а предохранитель: он размыкается по фактическим
    отказам рабочих запросов. Контрольный пинг врал бы в обе стороны — порт
    может отвечать, когда данные уже не идут, и наоборот.
    """
    out: Dict[str, str] = {}
    try:
        from services.market_data import iss_breaker_state
        st = iss_breaker_state()
        if st.get("open"):
            out["iss"] = (
                f"MOEX ISS не отвечает ({st.get('fails')} отказов подряд, "
                f"следующая проба через {st.get('reopen_in_sec'):.0f}с). "
                "Расписания, НКД и снапшот цен идут из кэша")
    except Exception as e:
        logger.debug("source_problems iss: %s", e)
    return out


# --- 2. свежесть ----------------------------------------------------------

def freshness_problems() -> Dict[str, str]:
    """Возраст того, на чём считаем. Протухший кэш отвечает бодро — и тем
    опасен: отказ виден сразу, а старые данные выглядят как свежие."""
    out: Dict[str, str] = {}
    from services.market_data import market_cache
    from services.paths import cache_path

    rates_date = market_cache.get("rates_date")
    if isinstance(rates_date, date):
        gap = (date.today() - rates_date).days
        if gap > RATES_STALE_DAYS:
            out["rates"] = (f"котировки ставок за {rates_date.isoformat()} — "
                            f"{gap} дн назад; кривые считаются по старым данным")

    age = _age_hours(cache_path("schedule_full_cache.json"))
    if age is not None and age > SCHEDULE_STALE_HOURS:
        out["schedules"] = (f"кэш расписаний купонов не обновлялся {age:.0f} ч — "
                            "утренний прогрев не проходит")

    at = market_cache.get("prewarm_at")
    if at:
        age_h = (time.time() - at) / 3600.0
        if age_h > PREWARM_STALE_HOURS:
            out["prewarm"] = (f"прогрев 09:00 не отмечался {age_h:.0f} ч — "
                              "день начат на вчерашних данных")
    return out


# --- 3. правдоподобие чисел -----------------------------------------------

def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def sanity_problems() -> Dict[str, str]:
    """Медиана Y-IDX по рынку против прошлого торгового дня.

    Смысл в том, что этот слой НЕ знает про конкретные баги. Спред всего рынка
    за сутки на десятки bps не переставляется, если цены стояли; когда такое
    видно — сломался расчёт, а не рынок. Именно так выглядела авария 10.09
    (медиана уехала на ~116 bps при неподвижных ценах), и точно так же
    выглядела бы следующая, ещё неизвестная.

    Сравнение — ПО БУМАГАМ (медиана разниц), а не разница медиан: состав
    витрины меняется, и вторая величина скакала бы от одного этого.
    """
    out: Dict[str, str] = {}
    try:
        from services.market_data import market_cache
        from services.spread_history import previous_day_spreads
        cur = market_cache.get("universe_metrics") or {}
        if not cur:
            return out
        prev = previous_day_spreads()
        if not prev:
            return out

        d_yidx, d_price = [], []
        for isin, m in cur.items():
            if not isinstance(m, dict):
                continue
            p = prev.get(isin)
            if not p:
                continue
            y_now, y_was = m.get("y_idx_bps"), p.get("y_idx")
            px_now, px_was = m.get("last"), p.get("price_pct")
            if None in (y_now, y_was, px_now, px_was):
                continue
            d_yidx.append(y_now - y_was)
            d_price.append(abs(px_now - px_was))

        if len(d_yidx) < SANITY_MIN_BONDS:
            return out
        m_y, m_p = _median(d_yidx), _median(d_price)
        if m_y is None or m_p is None:
            return out
        if abs(m_y) > SANITY_YIDX_JUMP_BPS and m_p < SANITY_PRICE_QUIET_PCT:
            out["yidx_jump"] = (
                f"медиана Y-IDX по рынку сдвинулась на {m_y:+.0f} bps "
                f"({len(d_yidx)} бумаг), а цены стояли (медиана |Δцены| "
                f"{m_p:.2f} п.п.) — похоже на поломку расчёта, а не на рынок")
    except Exception as e:
        logger.debug("sanity_problems: %s", e)
    return out


# --- 4. движок ------------------------------------------------------------

# Предыдущая длина очереди и сколько проверок подряд она не таяла.
_dirty_seen: dict = {"value": None, "stuck": 0}


def engine_problems() -> Dict[str, str]:
    """Справляется ли конвейер. Вне торговых часов тишина законна."""
    out: Dict[str, str] = {}
    try:
        from services import universe_stream
        st = universe_stream.stats()
    except Exception as e:
        logger.debug("engine_problems: %s", e)
        return out

    ctx = st.get("ctx") or 0
    no_acc = st.get("ctx_no_accrued") or 0
    if ctx and no_acc / ctx > ACCRUED_MISSING_SHARE:
        out["accrued"] = (f"биржевой НКД недоступен у {no_acc} из {ctx} бумаг — "
                          "спреды считаются по графику купонов (оценка)")

    if not trading_hours():
        return out

    rate = st.get("rate") or {}
    rows = rate.get("rows_per_min")
    if rows is not None and rows < ENGINE_MIN_ROWS_PER_MIN:
        out["engine_rate"] = (f"движок считает {rows} строк/мин "
                              f"({rate.get('row_ms')} мс/шт) — конвейер голодает")

    dirty = st.get("dirty") or 0
    prev = _dirty_seen["value"]
    # тает — сбрасываем счётчик, сколько бы ни было: движок справляется
    if prev is not None and dirty < prev:
        _dirty_seen["stuck"] = 0
    elif dirty > ENGINE_MAX_DIRTY:
        _dirty_seen["stuck"] += 1
    else:
        _dirty_seen["stuck"] = 0
    _dirty_seen["value"] = dirty

    if dirty > ENGINE_MAX_DIRTY and _dirty_seen["stuck"] >= ENGINE_DIRTY_STUCK_CHECKS:
        out["engine_queue"] = (f"очередь пересчёта {dirty} бумаг не уменьшается — "
                               "витрина отстаёт от рынка")
    return out


def all_problems() -> Dict[str, str]:
    """Все четыре вопроса разом — то, что зовёт сторож в api.main."""
    out: Dict[str, str] = {}
    for fn in (source_problems, freshness_problems, sanity_problems, engine_problems):
        try:
            out.update(fn())
        except Exception as e:
            logger.warning("data_health %s: %s", fn.__name__, e)
    return out
