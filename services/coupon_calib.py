"""Формула фиксинга купона флоатера: парсер текста проспекта (Cbonds колонка
«Купон») + эмпирический калибратор из реализованных выплат.

Два типа КС/RUONIA-флоатеров:
  • point   — ставка = индекс на дату (начало периода − lag), одна на период;
  • average — дневной ресет: каждый день Di ставка = индекс(Di − lag);
              купон = среднее по дням периода (RUONIA-стиль и большинство КС-2024+).
Лаг бывает в календарных («на 7-й день, предшествующий дате Di») и РАБОЧИХ
(«на 3-й рабочий день, предшествующий дате начала периода») днях — lag_unit.

Приоритет источников (services.ref_data.coupon_formula):
manual > парсер проспекта (авторитетно) > калибратор (фолбэк по истории купонов).
Калибратор: ставка прошлого купона = value·365/(days·face); наблюдённый индекс =
ставка − маржа; перебор mode×lag, лучший фит с ошибкой < порога.

Спека применяется к текущему/будущим незафиксированным купонам для точной
проекции: прошлые дни периода — факт ЦБ, будущие — форвард-прогноз."""
from __future__ import annotations
import re
from datetime import date, timedelta
from typing import Optional, Callable, List, Tuple

from services import cbr

_MAX_LAG = 12
_ERR_TOL_PP = 0.10     # порог средней ошибки, п.п.
_cache: dict = {}


# ── Парсер формулы из проспекта (Cbonds «Купон») ────────────────────────────
# Контексты, где «предшествующий» относится не к фиксингу купона
_SKIP_CTX = ("книги заявок", "сбора заявок", "открытия книги", "размещени",
             "оферт", "выкуп")

# Числительные прописью → номер дня лага. Часть проспектов пишут лаг словом
# («второй рабочий день», «предпоследний рабочий день»), а не цифрой.
# предпоследний = 2-й с конца = лаг 2; последний = лаг 1 (рыночная конвенция).
_WORDNUM = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "четвёрт": 4, "пят": 5,
            "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
            "предпоследн": 2, "последн": 1}
_WORDNUM_ALT = "|".join(sorted(_WORDNUM, key=len, reverse=True))

# «N-й [рабочий] день» перед «предшествующ» — цифрой ИЛИ прописью.
_LAG_RE = re.compile(
    r"(\d+)\s*-?\s*(?:й|ый|ий|ой|го)?\s*(?:\([^)]{0,30}\))?\s*"
    r"(рабоч\w*)?\s*(?:календарн\w*\s*)?(?:день|дня|дней)")
_LAG_WORD_RE = re.compile(
    r"(" + _WORDNUM_ALT + r")\w*\s*(рабоч\w*)?\s*(?:календарн\w*\s*)?(?:день|дня|дней)")

# point-anchor: начало купонного периода. «дате окончания (j-1)/предыдущего
# периода» = началу текущего → тоже point (лаг задан явно цифрой/словом).
# «началу i-го купонного», «началу купонного», «дате начала …» — индекс периода
# (i-го/j-го/n-го) допускается между «началу» и «купонного».
_POINT_ANCHOR = re.compile(r"дат[еы]\s+начала|дню\s+начала|днкп|"
                           r"начал[ау]\s+(?:\S+\s+){0,2}купонного|"
                           r"дат[еы]\s+окончани|окончани\w*\s+предыдущ")
_AVG_ANCHOR = re.compile(r"кажд\w+|дате\s+d\w?|календарной\s+дате")
_AVG_GLOBAL = re.compile(r"на\s+кажд(ую|ый)\s+(календарн\w+\s+)?(дату|день)|"
                         r"каждой\s+календарной\s+дате|кажд\w+\s+(календарн\w+\s+)?"
                         r"(день|дня|дней)\s+купонного|действующ\w*\s+на\s+кажд\w+")


def _lag_from(back: str):
    """Лаг из хвоста текста перед «предшествующ»: (lag:int, unit:str) | None.
    Цифра приоритетнее прописи; ближайшее к «предшествующ» вхождение."""
    lm = None
    for cand in _LAG_RE.finditer(back):
        lm = cand
    if lm is not None:
        return int(lm.group(1)), ("work" if lm.group(2) else "cal")
    wm = None
    for cand in _LAG_WORD_RE.finditer(back):
        wm = cand
    if wm is not None:
        key = next((k for k in _WORDNUM if wm.group(1).startswith(k)), None)
        if key is not None:
            return _WORDNUM[key], ("work" if wm.group(2) else "cal")
    return None


_parse_cache: dict = {}


def parse_prospectus_formula(text: str) -> Optional[dict]:
    """Текст формулы купона (проспект) → {'mode','lag','lag_unit'[,'capped']} | None.

    point:   «…на N-й [рабочий] день, предшествующий дате начала купонного периода»
    average: «…за N-й день, предшествующий дате Di» (Di — каждая дата периода) /
             «предшествующий каждой календарной дате» / «на каждую дату Di».
    Ошибочно НЕ матчим фиксинг первого купона по книге заявок (_SKIP_CTX).
    Валидация на юниверсе (267 неамортиз. бумаг, 4 последних купона vs факт ЦБ):
    медиана |ошибки| 1.2bps, p90 4.5bps, >25bps — 3 бумаги."""
    if not text:
        return None
    key = hash(text)
    if key in _parse_cache:
        cached = _parse_cache[key]
        return dict(cached) if cached else None
    res = _parse_prospectus_formula(text)
    _parse_cache[key] = dict(res) if res else None
    return res


def _parse_prospectus_formula(text: str) -> Optional[dict]:
    tl = text.replace("&hellip;", "…").replace("\xa0", " ").lower()
    best_point = best_avg = None
    for m in re.finditer(r"предшествующ\w*", tl):
        back = tl[max(0, m.start() - 90):m.start()]
        fwd = tl[m.end():m.end() + 110]
        if any(s in back + " " + fwd for s in _SKIP_CTX):
            continue
        got = _lag_from(back)               # цифра ИЛИ пропись
        if got is None:
            continue
        lag, unit = got
        if lag > 30:                        # мусорный матч (номер купона и т.п.)
            continue
        if _POINT_ANCHOR.search(fwd):
            best_point = best_point or {"mode": "point", "lag": lag, "lag_unit": unit}
        elif _AVG_ANCHOR.search(fwd) or _AVG_GLOBAL.search(tl):
            best_avg = best_avg or {"mode": "average", "lag": lag, "lag_unit": unit}
    # кэп/флор купона: MIN(КС+m; X%) / «не более X%» → потолок ставки;
    # MAX(…; Y%) / «не менее Y%» → пол ставки. Достаём ЧИСЛО (%, годовых) для
    # клэмпа купонной проекции. Берём самый связывающий: min(потолков)/max(полов).
    _NUM = r"([\d]+(?:[.,]\d+)?)\s*%"

    def _nums(pattern: str) -> list:
        vals = []
        for m in re.finditer(pattern, tl):
            try:
                vals.append(float(m.group(1).replace(",", ".")))
            except (ValueError, TypeError):
                pass
        return vals

    # ЖАДНЫЙ .* до ПОСЛЕДНЕГО ';' — кэп = аргумент после последнего разделителя,
    # чтобы вложенный MIN(MAX(КС;0);X%) брал X, а не внутренний 0.
    cap_vals = (_nums(r"min\s*\(.*;\s*" + _NUM)
                + _nums(r"(?:не\s+более|не\s+выше|но\s+не\s+прев\w*)\s*(?:чем\s+)?" + _NUM))
    floor_vals = (_nums(r"max\s*\(.*;\s*" + _NUM)
                  + _nums(r"(?:не\s+менее|не\s+ниже)\s*(?:чем\s+)?" + _NUM))
    cap_pct = min(cap_vals) if cap_vals else None
    floor_pct = max(floor_vals) if floor_vals else None
    capped = bool(cap_vals or floor_vals
                  or re.search(r"\bmin\s*\(|\bmax\s*\(|не\s+более|не\s+выше|не\s+менее|не\s+ниже|но\s+не\s+прев", tl))

    out = best_point or best_avg           # «дате начала» специфичнее — приоритет
    if out is None:
        # СРЕДНЕЕ ПО СДВИНУТОМУ ПЕРИОДУ: «среднее … RUONIA за период,
        # начинающийся за N дней до даты начала … заканчивающийся за M дней до
        # даты окончания купонного периода» → окно [s−N, e−N] = ровно то, что
        # даёт _rate_avg(s,e,lag=N). Дневной ресет, лаг N. (23 RUONIA-бумаги
        # шли сюда с лагом 0 — «за N дней до» не ловилось петлёй «предшествующ».)
        mw = re.search(r"начинающ\w*ся\s+за\s+(\d+)\s+(рабоч\w*\s+)?"
                       r"(?:календарн\w*\s+)?дн\w+\s+до\s+дат\w+\s+начал", tl)
        # СРЕДНЕЕ ПО ФИКСИР. ОКНУ [T−a; T−b] назад от даты начала: «за период
        # Т-37 дня - Т-7 дня». Точного окна модель не хранит; гладкий overnight
        # RUONIA ⇒ среднее ≈ точечный фиксинг в СЕРЕДИНЕ окна, лаг (a+b)/2.
        mf = re.search(r"за\s+период\s+[тt]\s*[-–]\s*(\d+)\s*(?:кал\w*\s*)?дн\w*"
                       r"\s*[-–—]\s*[тt]\s*[-–]\s*(\d+)", tl)
        if mw:
            out = {"mode": "average", "lag": int(mw.group(1)),
                   "lag_unit": "work" if mw.group(2) else "cal"}
        elif mf:
            a, b = int(mf.group(1)), int(mf.group(2))
            out = {"mode": "point", "lag": round((a + b) / 2), "lag_unit": "cal"}
        # без лага: «действующая на дату начала купонного периода»
        elif re.search(r"действующ\w*(\s+по\s+состоянию)?\s+на\s+дату\s+начала", tl):
            out = {"mode": "point", "lag": 0, "lag_unit": "cal"}
        # average-global без «предшествующ»: «КС, действующая на КАЖДЫЙ
        # календарный день купонного периода» → дневной ресет, лаг 0.
        elif _AVG_GLOBAL.search(tl):
            out = {"mode": "average", "lag": 0, "lag_unit": "cal"}
        # «среднее значение … за … период» без явного лага
        elif re.search(r"средн\w+\s+(значени|арифметическ)", tl):
            out = {"mode": "average", "lag": 0, "lag_unit": "cal"}
    # кэп/флор биндится к бумаге даже если режим фиксинга не распознан
    if out is None and (cap_pct is not None or floor_pct is not None):
        out = {}
    if out is not None:
        if capped:
            out["capped"] = True
        if cap_pct is not None:
            out["cap_pct"] = cap_pct
        if floor_pct is not None:
            out["floor_pct"] = floor_pct
    return out or None


def _obs_date(d: date, lag: int, unit: str) -> date:
    """Дата наблюдения индекса: d − lag календарных или РАБОЧИХ дней."""
    if unit != "work" or lag <= 0:
        return d - timedelta(days=lag)
    try:
        from valuation import _is_settlement_day_off as _off
    except Exception:
        def _off(x): return x.weekday() >= 5
    cur, left = d, lag
    while left > 0:
        cur -= timedelta(days=1)
        if not _off(cur):
            left -= 1
    return cur


import bisect

# Индекс истории для O(log n) поиска: (dates[], rates[]) по base, кэш на день.
_idx_cache: dict = {}


def _index(base: str):
    hist = cbr.ruonia_history() if base == "RUONIA" else cbr.ks_history()
    key = (base, len(hist), hist[-1][0] if hist else None)
    cached = _idx_cache.get(base)
    if cached and cached[0] == key:
        return cached[1], cached[2]
    dates = [d for d, _ in hist]
    rates = [r for _, r in hist]
    _idx_cache[base] = (key, dates, rates)
    return dates, rates


def index_history(base: str):
    """Публичная точка I/O: история индекса (dates[], rates_pct[]) для инжекции
    в расчётные функции (period_index_pct/calibrate/projected_ks_pct, параметр
    idx). Service-слой зовёт её ОДИН раз на запрос и передаёт результат вниз —
    ядро не ходит в сеть само, а сбой фетча виден на границе (→ warnings),
    а не глотается внутри прайсинга."""
    return _index(base)


def _rate_at(idx, d: date) -> Optional[float]:
    """idx = (dates, rates), отсортированы по дате. Последняя ставка ≤ d (bisect)."""
    dates, rates = idx
    i = bisect.bisect_right(dates, d) - 1
    return rates[i] if i >= 0 else None


def _rate_avg(idx, s: date, e: date, lag: int) -> Optional[float]:
    tot, n, cur = 0.0, 0, s
    while cur < e:
        k = _rate_at(idx, cur - timedelta(days=lag))
        if k is not None:
            tot += k
            n += 1
        cur += timedelta(days=1)
    return tot / n if n else None


def _face_on(face: float, amorts: list, start: date, calc_date: date) -> float:
    """Номинал, действовавший на дату start ≤ calc_date: текущий остаток +
    амортизации, выплаченные в (start, calc_date]."""
    add = 0.0
    for a in amorts or []:
        d = a.get("date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except (ValueError, TypeError):
                continue
        if isinstance(d, date) and a.get("value") is not None and start < d <= calc_date:
            add += float(a["value"])
    return face + add


def _past_rows(coupons: list, margin_pct: float, face: float, calc_date: date,
               amorts: list = None):
    """Наблюдённый индекс прошлых периодов: ставка_купона − маржа, %.

    face — ТЕКУЩИЙ остаток номинала; для каждого периода откатываем его назад по
    графику амортизаций (_face_on). Раньше все периоды делились на один и тот же
    face, из-за чего у амортизируемых бумаг наблюдённые ставки завышались тем
    сильнее, чем старше купон, и калибровка либо не проходила порог _ERR_TOL_PP,
    либо подбирала неверный режим/лаг.
    """
    rows = []
    for c in coupons or []:
        s, e, v = c.get("start"), c.get("end"), c.get("value")
        if not s or not e or v is None:
            continue
        try:
            s = date.fromisoformat(s) if isinstance(s, str) else s
            e = date.fromisoformat(e) if isinstance(e, str) else e
        except (ValueError, TypeError):
            continue
        if s >= calc_date:
            continue
        face_p = _face_on(face, amorts, s, calc_date)
        if face_p <= 0:
            continue
        days = (e - s).days or 1
        rows.append((s, e, float(v) / face_p * 365.0 / days * 100.0 - margin_pct))
    rows.sort(key=lambda r: r[1])          # порядок `coupons` не гарантирован
    return rows[-8:]


def calibrate(isin: str, coupons: list, margin_pct: float, face: float,
              calc_date: date, base: str = "KEYRATE", amorts: list = None,
              idx=None) -> Optional[dict]:
    """Спека формулы {'mode':'point'|'average','lag':int} по прошлым купонам.
    base='KEYRATE' — факт КС ЦБ; 'RUONIA' — дневной RUONIA (обычно average).
    face — текущий остаток номинала; amorts — график погашений (для отката
    номинала на дату каждого прошлого периода, см. _past_rows).
    idx — инжектированная история индекса (index_history); None → сам фетчит."""
    rows = _past_rows(coupons, margin_pct, face, calc_date, amorts)
    # Ключ кэша включает отпечаток данных (число прошлых купонов + дата последнего):
    # раньше кэшировалось по (isin, base) НАВСЕГДА — новые купоны/сменившийся manual
    # не пересчитывали спеку до рестарта процесса.
    ck = (isin, base, len(rows), rows[-1][1] if rows else None)
    if ck in _cache:
        return _cache[ck]
    if idx is None:
        idx = _index(base)
    spec = None
    if len(rows) >= 2 and idx[0]:
        best = None  # (err, mode, lag)
        for lag in range(0, _MAX_LAG + 1):
            e_pt, e_av, n = 0.0, 0.0, 0
            for s, e, obs in rows:
                kp = _rate_at(idx, s - timedelta(days=lag))
                ka = _rate_avg(idx, s, e, lag)
                if kp is None or ka is None:
                    continue
                e_pt += abs(kp - obs)
                e_av += abs(ka - obs)
                n += 1
            if not n:
                continue
            for mode, err in (("point", e_pt / n), ("average", e_av / n)):
                if best is None or err < best[0]:
                    best = (err, mode, lag)
        if best and best[0] < _ERR_TOL_PP:
            spec = {"mode": best[1], "lag": best[2], "err_pp": round(best[0], 4), "base": base}
    _cache[ck] = spec
    return spec


def period_index_pct(isin: str, base: str, coupons: list, face: float,
                     start: date, end: date, calc_date: date,
                     fwd_pct: Callable[[date], float],
                     amorts: list = None, idx=None) -> Optional[float]:
    """Индекс-компонента ставки купона (%) для НАЧАВШЕГОСЯ периода (start ≤ calc):
    спека формулы выпуска (manual > калибратор из истории купонов) →
    projected_ks_pct (прошлые дни — факт ЦБ, будущие — форвард). Фолбэк —
    точечный фиксинг КС на start (прежнее поведение pricing-пайплайнов).
    face — ТЕКУЩИЙ остаток номинала (не номинал периода): откат по amorts делает
    сам калибратор. Все три пайплайна (valuation, zspread, display) обязаны
    передавать одно и то же, иначе спека калибруется по разным данным.
    None → спеки нет и фолбэк неприменим (RUONIA без спеки) — звать форвард-проекцию.

    Единая точка для pricing (valuation, zspread) и display (services.cashflow):
    один и тот же купон во всех пайплайнах."""
    if start > calc_date:
        return None
    spec = None
    try:
        from services.ref_data import coupon_formula
        s = coupon_formula(isin, coupons, face=face, calc_date=calc_date, amorts=amorts,
                           idx=idx)
        if s.get("coupon_mode") is not None:
            spec = {"mode": s["coupon_mode"], "lag": s.get("fixing_lag") or 0,
                    "lag_unit": s.get("fixing_lag_unit") or "cal", "base": base}
    except Exception:
        spec = None
    if spec is not None:
        return projected_ks_pct(spec, start, end, calc_date, fwd_pct, idx=idx)
    if base == "KEYRATE":
        if idx is not None:
            k = _rate_at(idx, start)           # история хранит проценты
            return float(k) if k is not None else None
        from services.cbr import ks_rate_at
        k = ks_rate_at(start)
        return k * 100.0 if k is not None else None
    return None


# Допуск запаздывания истории индекса: обычный лаг публикации (выходные/до
# обновления) — до этого порога carry-forward последнего значения корректен
# (RUONIA/КС держат ставку предыдущего рабочего дня). За порогом история СТЕЙЛ:
# день с obs > last_hist больше НЕ считается «фактом» — уходит на форвард, иначе
# стейл-ставка молча текла бы в купон начавшегося периода (аудит F1).
_HIST_STALE_GRACE_DAYS = 4


def _realized(idx, obs: date, calc_date: date) -> bool:
    """obs — реализованный факт (а не проекция), если он в прошлом ОТНОСИТЕЛЬНО
    calc_date И ЛОКАЛЬНО покрыт историей: ближайшая известная дата ≤ obs отстоит
    не больше чем на grace. Проверяем ЛОКАЛЬНОЕ покрытие, а не только max-дату —
    иначе внутренняя ДЫРА в истории (напр. RC_F до 08.07, live с 21.07) была бы
    невидима: bisect тянул бы 08.07 вперёд, а last=21.07 говорил бы «факт».
    Выходной/праздник у края (obs−предыдущий фиксинг ≤ grace) остаётся фактом
    (carry-forward корректен); настоящая дыра/застой уходит на форвард."""
    if obs > calc_date:
        return False
    dts = idx[0] if idx else None
    if not dts:
        return False
    i = bisect.bisect_right(dts, obs) - 1     # последняя дата истории ≤ obs
    if i < 0:
        return False
    return (obs - dts[i]).days <= _HIST_STALE_GRACE_DAYS


def projected_ks_pct(spec: dict, start: date, end: date, calc_date: date,
                     fwd_pct: Callable[[date], float], idx=None) -> float:
    """Компонента ставки купона (%) по спеке: прошлые дни — факт ЦБ (КС/RUONIA),
    будущие ИЛИ не покрытые стейл-историей — fwd_pct(date). point → одна дата;
    average → среднее по дням. lag_unit='work' — лаг в рабочих днях.
    idx — инжектированная история (index_history); None → сам фетчит."""
    if idx is None:
        idx = _index(spec.get("base", "KEYRATE"))
    lag = spec.get("lag", 0)
    unit = spec.get("lag_unit", "cal")
    if spec.get("mode") == "point":
        fix = _obs_date(start, lag, unit)
        return (_rate_at(idx, fix) if _realized(idx, fix, calc_date) else fwd_pct(fix)) or 0.0
    tot, n, cur = 0.0, 0, start
    while cur < end:
        obs = _obs_date(cur, lag, unit)
        k = _rate_at(idx, obs) if _realized(idx, obs, calc_date) else fwd_pct(obs)
        if k is not None:
            tot += k
            n += 1
        cur += timedelta(days=1)
    return (tot / n) if n else 0.0
