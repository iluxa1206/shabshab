"""ЛИНКЕРЫ RUONIA: облигации с ИНДЕКСИРУЕМЫМ НОМИНАЛОМ и фиксированной ставкой.

Что это. Купон задан константой (ВЭБ.РФ ПБО-002Р-58: 1.85% годовых), а
номинальная стоимость растёт КАЖДЫЙ ДЕНЬ по официальному индексу RUONIA ЦБ
(RuoniaSV). Экономически держатель получает RUONIA + 185 bps, то есть это
флоатер, а не фикс: MOEX и относит такие выпуски к виду «Линкер/облигации с
индексируемым номиналом».

Почему нужен отдельный слой.
  • Дискавери ловит флоатеров по признаку «есть будущий купон с value=None».
    У линкера ВСЕ купоны имеют сумму (эхо по сегодняшнему номиналу), поэтому он
    проваливался в negative-кэш как фикс и оседал во вкладке ФИКСЫ с ложным YTM.
  • Отличить линкер RUONIA от прочих линкеров по флагу «номинал растёт» нельзя:
    под тем же видом MOEX торгуются золотые (SELGOLD/PolyusZL), серебряный
    (SELSILV01) и ИПЦ-линкеры. Разделяем СВЕРКОЙ РОСТА номинала с официальным
    индексом RUONIA — у чужой базы он не сойдётся и на порядок.

Прайсинг живёт в core.valuation.build_cashflows_to_maturity (ветка `linker`):
поток строится в номинальных рублях с ростом номинала, дисконт остаётся общим
для RUONIA-бумаг, и solver отдаёт реальную доходность = спред к RUONIA.
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Единственная поддержанная база индексации номинала. Строка (а не bool) —
# в реестре поле хранит имя базы: золотые/ИПЦ-линкеры сюда не попадают, но
# место под них в схеме уже есть.
RUONIA = "RUONIA"

# Допуск сверки «рост номинала vs индекс RUONIA», доли. 0.5% с запасом
# перекрывает округление номинала до копеек, лаг индексации в пару дней и
# расхождение конвенций — и на три порядка меньше отрыва чужих баз
# (золото ×12, ИПЦ ×1.4 при RUONIA ×1.55 за тот же срок).
_TOL = 0.005

# Минимальный рост, ниже которого сверять нечего: свежий выпуск (несколько дней
# от размещения) не отличим от фикса ничем, кроме шума округления.
_MIN_GROWTH = 0.002


def face_grow_provider(calc_date: date):
    """Провайдер роста индекса номинала для ядра: fn(frm, to, fwd_pct) → множитель.

    I/O-граница: история индекса ЦБ фетчится ЗДЕСЬ (раз на запрос), ядро
    получает готовое замыкание — тот же контракт, что у index_pct_fn
    (services.valuation._index_provider). Реализация — общий дневной путь
    coupon_calib._index_grow_cached: факт ЦБ до последней публикации, дальше
    ступени форвардной кривой. Возвращает None, если истории нет: ядро тогда
    пометит деградацию, а не посчитает молча неверно.
    """
    try:
        from services.coupon_calib import _index_grow, _index_grow_cached, index_history
        idx = index_history(RUONIA)
        if not idx[0]:
            return None
    except Exception as e:
        logger.warning("провайдер роста номинала недоступен: %s", e)
        return None

    def _grow(frm: date, to: date, fwd_pct) -> float:
        if to == frm:
            return 1.0
        if to > frm:
            return _index_grow_cached(frm, to, calc_date, fwd_pct, idx)
        # НАЗАД ВО ВРЕМЕНИ — обратный множитель по реализованному ряду. Нужен
        # для НАЧАВШЕГОСЯ купонного периода: он начислялся на номинал, который
        # тогда был МЕНЬШЕ сегодняшнего, и принять прошлые дни за сегодняшние
        # значит завысить купон (на годовом периоде — до 4%). Считаем НЕ через
        # общий кэш пути: тот заякорен на одну дату и делится всем универсом,
        # а пере-якорение на каждый старт периода превращает его в промах
        # (именно этот цикл когда-то давал фризы ядра). Отрезок здесь не длиннее
        # одного купонного периода, и линкеров в универсе единицы.
        g = _index_grow(to, frm, calc_date, fwd_pct, idx)
        return 1.0 / g if g else 1.0

    return _grow


def _fixed_rate_pct(coupons) -> Optional[float]:
    """Единая ставка купона выпуска, % годовых. None — ставка не одна (обычный
    флоатер с разными фиксингами) или её нет."""
    rates = {round(float(c["valueprc"]), 6) for c in (coupons or [])
             if c.get("valueprc") not in (None, "")}
    if len(rates) != 1:
        return None
    r = rates.pop()
    return r if r > 0 else None


def expected_growth(frm: date, to: date) -> Optional[float]:
    """Рост официального индекса RUONIA за [frm, to] по ОПУБЛИКОВАННОМУ ряду.
    (множитель, дата последнего учтённого дня) — ряд ЦБ отстаёт от сегодня на
    несколько дней, и хвост честно не покрыт. None — ряда нет."""
    from services.coupon_calib import ruonia_index_levels
    levels, last = ruonia_index_levels()
    if not levels or not last:
        return None
    end = min(to, last)
    i0 = levels.get(frm)
    if i0 is None:                      # выходной/праздник: ближайший ранний день
        prior = [d for d in levels if d <= frm]
        i0 = levels[max(prior)] if prior else None
    i1 = levels.get(end)
    if i1 is None:
        prior = [d for d in levels if d <= end]
        i1 = levels[max(prior)] if prior else None
    if not i0 or not i1 or i0 <= 0:
        return None
    return i1 / i0


def is_ruonia_linked(coupons, face_now: Optional[float],
                     calc_date: Optional[date] = None) -> bool:
    """Индексируется ли номинал выпуска по RUONIA.

    Вход — купоны MOEX bondization (нужны initial_face, face и startdate) и
    текущий биржевой номинал. Правило: ставка купона одна на весь график
    (иначе это обычный флоатер), номинал ВЫРОС относительно первоначального, и
    рост СХОДИТСЯ с официальным индексом RUONIA за тот же срок в пределах
    допуска. Хвост, не покрытый публикацией ряда ЦБ, добавляется к верхней
    границе по последней ставке — иначе свежий индекс всегда «обгонял» бы ряд.
    """
    calc_date = calc_date or date.today()
    if _fixed_rate_pct(coupons) is None:
        return False
    starts = sorted(c["start"] for c in (coupons or []) if c.get("start"))
    inits = {float(c["initial_face"]) for c in (coupons or [])
             if c.get("initial_face") not in (None, "")}
    if not starts or len(inits) != 1:
        return False
    init = inits.pop()
    if not init or not face_now or face_now <= 0:
        return False
    ratio = face_now / init
    if ratio - 1.0 < _MIN_GROWTH:
        return False
    try:
        issue = date.fromisoformat(starts[0]) if isinstance(starts[0], str) else starts[0]
    except (ValueError, TypeError):
        return False

    from services.coupon_calib import ruonia_index_levels
    _levels, last = ruonia_index_levels()
    exp = expected_growth(issue, calc_date)
    if exp is None:
        return False
    # непокрытый хвост ряда ЦБ: до 1.5× текущей ставки в день (запас на разгон)
    tail_days = max((calc_date - last).days, 0) if last else 0
    try:
        from services import cbr
        rate = (cbr.ruonia_history() or [(None, 0.0)])[-1][1] or 0.0
    except Exception:
        rate = 0.0
    tail = exp * (rate / 100.0) * tail_days / 365.0 * 1.5
    return (exp * (1 - _TOL)) <= ratio <= (exp + tail) * (1 + _TOL)


def grow_fn_for(ref, calc_date: date):
    """face_grow_fn для конкретной бумаги: None у всех, кроме линкеров.

    Обёртка нужна, чтобы каждый потребитель потока (карточка, z-спред, календарь
    выплат) не повторял проверку и не забыл её — забытый провайдер стоит не
    точности, а МЕТОДИКИ: линкер молча посчитался бы на постоянном номинале.
    """
    return face_grow_provider(calc_date) if getattr(ref, "face_index", None) else None
