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
    r"(\d+)\s*-?\s*(?:й|ый|ий|ой|го|и)?\s*(?:\([^)]{0,30}\))?\s*"   # «и» — OCR «7-и день»
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
# NB: «дате выплаты» как point-якорь УБРАН — эмпирика (калибратор, err≈0) показала,
# что такие RUONIA-бумаги на деле average (дневной ресет), а point давал ~0.8пп
# ошибки. Без якоря спек берёт калибратор из истории купонов.
# avg-якоря: Di/Dj (лат.), q-дата Σ-формул, ДКПj (кир., «дата купонного периода»,
# в отличие от ДНКП = дата НАЧАЛА → point)
_AVG_ANCHOR = re.compile(r"кажд\w+|дате\s+d\w?|календарной\s+дате|"
                         r"дате\s+q\b|дате\s+дкп")
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
    # Явные окончания причастия, а не \w* — в выгрузке Cbonds пробел часто теряется
    # («предшествующийдате Di»), и жадный \w* съедал бы «дате», унося якорь фиксинга
    # из fwd → mode/lag не распознавались (десятки RUONIA/КС-бумаг).
    for m in re.finditer(r"предшествующ(?:его|ему|ими|ий|ей|ая|ую|ее|ие|их|им|ем)?", tl):
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
        # Якорь фиксинга — тот, что БЛИЖЕ к «предшествующ»: в хвосте обычно идёт
        # определение ДРУГИХ дат («Di+1 - дата, следующая за датой начала i-го
        # купонного периода»), и дальний point-якорь перебивал верный avg («дате D»).
        # Банк Синара С01: point давал 0.91пп ошибки против 0.015пп у average.
        m_pt, m_av = _POINT_ANCHOR.search(fwd), _AVG_ANCHOR.search(fwd)
        if m_pt and (m_av is None or m_pt.start() < m_av.start()):
            best_point = best_point or {"mode": "point", "lag": lag, "lag_unit": unit}
        elif m_av or _AVG_GLOBAL.search(tl):
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
                + _nums(r"(?:не\s+более|не\s+выше|но\s+не\s+прев\w*|"
                        r"не\s+мож\w+\s+быть\s+бол\w+)\s*(?:чем\s+)?" + _NUM)
                # кэп без «%», числом+«процент(а)»: «не может быть более 21,30
                # (Двадцати…) процента годовых» — скобочную пропись пропускаем
                + _nums(r"(?:не\s+более|не\s+выше|не\s+мож\w+\s+быть\s+бол\w+)\s*"
                        r"([\d]+(?:[.,]\d+)?)\s*(?:\([^)]*\)\s*)?процент"))
    floor_vals = (_nums(r"max\s*\(.*;\s*" + _NUM)
                  + _nums(r"(?:не\s+менее|не\s+ниже)\s*(?:чем\s+)?" + _NUM))
    cap_pct = min(cap_vals) if cap_vals else None
    floor_pct = max(floor_vals) if floor_vals else None
    # capped только при извлечённом cap/floor ИЛИ явных «не более/менее» — голые
    # min(/max( исключены: max(…;0) в RUONIA-индекс-линкерах = пол 0% (не связывает),
    # спурьёзный capped помечал бы бумагу как ограниченную зря.
    capped = bool(cap_vals or floor_vals
                  or re.search(r"не\s+более|не\s+выше|не\s+менее|не\s+ниже|но\s+не\s+прев", tl))

    out = best_point or best_avg           # «дате начала» специфичнее — приоритет
    if out is None:
        # POINT «[по состоянию] на N [рабочий] день ДО даты начала» — Cbonds пишет
        # «до», а не «предшествующий», поэтому петля выше не ловила (десятки КС-бумаг).
        mp = re.search(r"на\s+(\d+)\s*-?\s*(?:й|ый|ий|ой|го)?\s*(рабоч\w*\s+)?"
                       r"(?:календарн\w*\s+)?(?:день|дня|дней)\s+до\s+дат\w+\s+начал", tl)
        if mp:
            out = {"mode": "point", "lag": int(mp.group(1)),
                   "lag_unit": "work" if mp.group(2) else "cal"}
    if out is None:
        # RUONIA-ИНДЕКСНЫЙ ЛИНКЕР: Rj = (IndexEnd_{j-N}/IndexStart_{j-N} − 1)·B/Tj + S,
        # где Index — компаунд-индекс RUONIA на N-й календарный день до Start/End
        # периода. Отношение индексов, аннуализированное = реализованная средняя
        # RUONIA за окно [Start−N, End−N] = сдвинутый период → average, лаг N.
        # (ВЭБ.РФ ПБО-002Р RUONIA-Индекс, Роснефть 005Р-01.) max(…;0) — пол 0%,
        # для RUONIA>0 не связывает, игнорируем.
        mi = re.search(r"индекс\w*\s+ruonia\s+для\s+(\d+)\s*-?\s*г?о?\s+"
                       r"календарн\w+\s+дн\w+,?\s+предшеств\w*\s+start", tl)
        if mi:
            out = {"mode": "average", "lag": int(mi.group(1)), "lag_unit": "cal"}
    if out is None:
        # СРЕДНЕЕ ПО СДВИНУТОМУ ПЕРИОДУ: «среднее … RUONIA за период,
        # начинающийся за N дней до даты начала … заканчивающийся за M дней до
        # даты окончания купонного периода» → окно [s−N, e−N] = ровно то, что
        # даёт _rate_avg(s,e,lag=N). Дневной ресет, лаг N. (23 RUONIA-бумаги
        # шли сюда с лагом 0 — «за N дней до» не ловилось петлёй «предшествующ».)
        mw = re.search(r"начинающ\w*ся\s+за\s+(\d+)\s+(рабоч\w*\s+)?"
                       r"(?:календарн\w*\s+)?дн\w+\s+до\s+дат\w+\s+начал", tl)
        # ...но если окно закрывается по ПРЕДЫДУЩЕМУ периоду («заканчивающийся за N
        # дней до даты окончания ПРЕДЫДУЩЕГО купонного периода»), то окно =
        # [prev_start−N, prev_end−N] = [start−period−N, start−N) — это avg_prev, а
        # НЕ окно текущего периода. Разница боевая: на этих 4 бумагах (РЖД 001P-26R/
        # 27R/28R, РСХБ БO-03-002P) average врал mean 0.5пп / max 3.2пп против
        # mean 0.03пп / max 0.22пп у avg_prev (аудит по bondsearch 27.07.2026).
        mw_prev = re.search(r"заканчивающ\w*ся\s+за\s+\d+\s+(?:рабоч\w*\s+)?"
                            r"(?:календарн\w*\s+)?дн\w+\s+до\s+дат\w+\s+окончани\w*\s+"
                            r"предыдущ\w*\s+купонн\w*\s+период", tl)
        # СРЕДНЕЕ ПО ФИКСИР. ОКНУ [T−a; T−b] назад от даты начала. Пишут по-разному:
        # «за период Т-37 дня - Т-7 дня», «от (Ti-7 до Ti-37)», «от Ti-7 до t=Ti-37».
        # Точного окна модель не хранит; гладкий overnight-RUONIA ⇒ среднее ≈
        # точечный фиксинг в СЕРЕДИНЕ окна, лаг (a+b)/2 (порядок a,b неважен).
        mf = re.search(r"[тt]i?\s*[-–]\s*(\d+)\s*(?:кал\w*\s*)?(?:дн\w*\s*)?"
                       r"(?:до|[-–—])\s*(?:t\s*=\s*)?[тt]i?\s*[-–]\s*(\d+)", tl)
        if mw:
            out = {"mode": "avg_prev" if mw_prev else "average",
                   "lag": int(mw.group(1)),
                   "lag_unit": "work" if mw.group(2) else "cal"}
        elif mf:
            # окно [T−a, T−b] назад от старта → среднее по предыдущему периоду со
            # сдвигом. lag = БЛИЖНИЙ офсет (min): для «Т-37..Т-7» lag=7, окно длиной
            # period заканчивается за 7д до старта. Точнее прежней аппроксимации
            # точечным фиксингом в середине окна (лаг (a+b)/2): на движениях ставки
            # midpoint-point врал до 0.5пп vs 0.03пп у точного окна (аудит Русагро).
            a, b = int(mf.group(1)), int(mf.group(2))
            out = {"mode": "avg_prev", "lag": min(a, b), "lag_unit": "cal"}
        # СРЕДНЕЕ ЗА ОКНО ПЕРЕД ДАТОЙ ОПРЕДЕЛЕНИЯ СТАВКИ: «среднеарифметическое
        # значение КС в течение N дней (включительно), ПРЕДШЕСТВУЮЩИХ Дате
        # определения новой ставки купона» — окно [start−N, start) перед стартом,
        # т.е. avg_prev с лагом 0 (у этих бумаг N ≈ длине периода: Альфа-Банк
        # Т2-CR, месячный купон, N=30). Раньше падало в average (окно ТЕКУЩЕГО
        # периода) — 0.31пп ошибки против 0.023пп у avg_prev.
        # Пишут двояко: «среднеарифметическое значение КС» (Т2-CR-03) либо через
        # частное «Rsd/SD, Rsd — СУММА ВСЕХ ЗНАЧЕНИЙ …, SD — количество дней»
        # (Т2-CR-04/05/06). «в\s*течение» — в выгрузке Cbonds пробел часто теряется
        # («втечение»), как и в других местах текста.
        elif re.search(r"(?:средн\w*(?:\s*арифметическ\w*)?\s*значени\w*|"
                       r"сумма\s*всех\s*значени\w*)"
                       r"[^.;]{0,220}?в\s*течение\s+\d+\s*(?:\([^)]*\)\s*)?дн\w+"
                       r"[^.;]{0,60}?предшествующ\w*\s*дат\w+\s*определени", tl):
            out = {"mode": "avg_prev", "lag": 0, "lag_unit": "cal"}
        # без лага: «действующая на дату начала купонного периода»
        elif re.search(r"действующ\w*(\s+по\s+состоянию)?\s+на\s+дату\s+начала", tl):
            out = {"mode": "point", "lag": 0, "lag_unit": "cal"}
        # ФИКСИНГ НА 1-Е ЧИСЛО МЕСЯЦА периода: «действующая по состоянию на 1-й
        # (первый) день [календарного] месяца, на который приходится дата начала»
        # (ИЖА ДОМ.РФ, ~10 бумаг). Раньше приближалось point lag 0 — фиксинг на
        # START периода вместо 1-го числа месяца давал систематику ~0.33пп при
        # движении КС внутри месяца. Теперь точный режим month_start:
        # obs = 1-е число месяца start (projected_ks_pct).
        elif re.search(r"на\s+1(?:-?й)?\s*(?:\([^)]*\)\s*)?день\s+"
                       r"(?:календарн\w*\s+)?месяца", tl):
            out = {"mode": "month_start", "lag": 0, "lag_unit": "cal"}
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


def fixing_probe_date(spec: dict, start: date) -> date:
    """Дата наблюдения индекса для старта периода по спеке — единая точка для
    бэктестов (verify_fixing_specs, bond_audit): month_start игнорирует лаг."""
    if spec.get("mode") == "month_start":
        return start.replace(day=1)
    return _obs_date(start, spec.get("lag") or 0, spec.get("lag_unit") or "cal")


# Маржа-лесенка: у части выпусков надбавка меняется по номерам купонов
# («S 1-7 = 2.5%, S8-21 = 4.6%» БинФарм; «13-18 купоны: Ci = R + 2,5%,
# 19-22: Ci = R + 3,5%» ТрансФин-М). Скалярный margin_bps реестра тогда врёт
# на всех периодах вне «своего» диапазона (до 2.1пп на купоне).
# Диапазон вида «S 1-7 = 2.5%» / «S8-21 = 4.6%».
_MS_S_RE = re.compile(r"s\s*(\d+)\s*[-–—]\s*(\d+)\s*=\s*([\d]+(?:[.,]\d+)?)\s*%")
# Диапазон вида «17-18 купоны: Ci = R + 3,25%» / «6-8 купоны - КС + 0%».
# Окно [^%;] не даёт '+' перетечь через чужой процент («MAX((Ij-100%)+4%» ИПЦ-
# линкеров не матчится); фикс-ступени («1-2 купоны - 12.75%») без '+' — мимо.
_MS_CPN_RE = re.compile(
    r"(\d+)\s*(?:[-–—]\s*(\d+))?\s*купон\w*\s*[:\-–—]?\s*"
    r"[^%;]{0,120}?\+\s*([\d]+(?:[.,]\d+)?)\s*%")


def parse_margin_schedule(text: str) -> Optional[list]:
    """Текст формулы купона → [{'from','to','bps'}] по номерам купонов (1-based)
    или None. Ловит только диапазоны с ЧИСЛОВОЙ маржой при базе; символьная
    надбавка («+ S» без числа рядом) остаётся на скалярном margin_bps реестра.
    bps=0 значим (реальные «КС + 0%» ступени)."""
    if not text:
        return None
    tl = text.replace("\xa0", " ").lower()
    # ИПЦ/инфляция/GCurve-линкер: купон не от КС/RUONIA — «маржа» в тексте
    # относится к другой базе, лесенка была бы ложной (Ситиматик, РОСНАНО)
    if re.search(r"ипц|индекс\w*\s+потребительск\w+\s+цен|инфляц|gcurve|кбд", tl):
        return None
    # БУКВЕННО-ИНДЕКСНАЯ лесенка: «Ci = MIN(Cr+6,0%; 24%) Cy = MIN(Cr+5,0%; 23%)
    # …, при этом i = 2, 3...12; y = 13, 14...24; k = 25, 26...36» (Джой 1P2).
    # Наивный range-матч брал «2-36 купоны → +6%» — первый '+' на весь диапазон
    # (2пп ошибки на хвосте). Буквы связывают маржу с диапазоном точно.
    ldefs = {m.group(1): round(float(m.group(2).replace(",", ".")) * 100)
             for m in re.finditer(
                 r"c([a-zа-я])\s*=\s*(?:min\s*\(\s*)?c?r\s*\+\s*"
                 r"([\d]+(?:[.,]\d+)?)\s*%", tl)}
    lrngs = {m.group(1): (int(m.group(2)), int(m.group(3)))
             for m in re.finditer(
                 r"([a-zа-я])\s*=\s*(\d+),\s*\d+\s*\.{3}\s*(\d+)", tl)}
    letter_steps = {lrngs[l]: bps for l, bps in ldefs.items() if l in lrngs}
    if len(letter_steps) >= 2:
        return [{"from": k[0], "to": k[1], "bps": v}
                for k, v in sorted(letter_steps.items())]
    steps = {}
    for m in _MS_S_RE.finditer(tl):
        a, b = int(m.group(1)), int(m.group(2))
        steps[(min(a, b), max(a, b))] = round(float(m.group(3).replace(",", ".")) * 100)
    for m in _MS_CPN_RE.finditer(tl):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        key = (min(a, b), max(a, b))
        if key not in steps:
            steps[key] = round(float(m.group(3).replace(",", ".")) * 100)
    if not steps:
        return None
    return [{"from": k[0], "to": k[1], "bps": v}
            for k, v in sorted(steps.items())]


def _obs_date(d: date, lag: int, unit: str) -> date:
    """Дата наблюдения индекса: d − lag календарных или РАБОЧИХ дней."""
    if unit != "work" or lag <= 0:
        return d - timedelta(days=lag)
    try:
        from core.valuation import _is_settlement_day_off as _off
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
    # дни дохода (s, e] — та же НКД-конвенция, что в average-ветке
    # projected_ks_pct: калибратор и проекция обязаны мерить одинаково,
    # иначе фит-лаг компенсирует сдвиг и разъезжается с парсер-лагом
    tot, n, cur = 0.0, 0, s + timedelta(days=1)
    while cur <= e:
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
              idx=None, fixed_mode: Optional[str] = None) -> Optional[dict]:
    """Спека формулы {'mode':'point'|'average','lag':int} по прошлым купонам.
    base='KEYRATE' — факт КС ЦБ; 'RUONIA' — дневной RUONIA (обычно average).
    face — текущий остаток номинала; amorts — график погашений (для отката
    номинала на дату каждого прошлого периода, см. _past_rows).
    idx — инжектированная история индекса (index_history); None → сам фетчит.
    fixed_mode — режим уже известен (парсер проспекта дал mode без лага):
    подбираем ТОЛЬКО лаг при этом режиме. Иначе лаг брался из лучшей пары
    (mode, lag) калибратора, возможно с ДРУГИМ mode — в прайсинг уходил гибрид
    «mode парсера + lag чужого фита» (до 0.5пп на купоне). Режимы вне перебора
    (avg_prev) не фитим — None, потребитель применяет дефолт."""
    rows = _past_rows(coupons, margin_pct, face, calc_date, amorts)
    modes = ("point", "average") if fixed_mode is None else (
        (fixed_mode,) if fixed_mode in ("point", "average") else ())
    if not modes:
        return None
    # Ключ кэша включает отпечаток данных (число прошлых купонов + дата последнего):
    # раньше кэшировалось по (isin, base) НАВСЕГДА — новые купоны/сменившийся manual
    # не пересчитывали спеку до рестарта процесса.
    ck = (isin, base, len(rows), rows[-1][1] if rows else None, fixed_mode)
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
                if mode not in modes:
                    continue
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
        if s.get("coupon_mode") is not None or s.get("avg_window_days"):
            spec = {"mode": s.get("coupon_mode"), "lag": s.get("fixing_lag") or 0,
                    "lag_unit": s.get("fixing_lag_unit") or "cal", "base": base,
                    "avg_window_days": s.get("avg_window_days")}
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
    # Единая параметризация: явное окно усреднения W дней, зафиксированное на
    # старте периода — окно [obs(start) − W, obs(start)). W=1 ≡ point,
    # W=длина периода ≡ avg_prev. Явное W из Справочника сильнее mode.
    w = spec.get("avg_window_days")
    if w:
        w_hi = _obs_date(start, lag, unit)
        if w <= 1:
            return (_rate_at(idx, w_hi) if _realized(idx, w_hi, calc_date) else fwd_pct(w_hi)) or 0.0
        tot, n, cur = 0.0, 0, w_hi - timedelta(days=int(w))
        while cur < w_hi:
            k = _rate_at(idx, cur) if _realized(idx, cur, calc_date) else fwd_pct(cur)
            if k is not None:
                tot += k
                n += 1
            cur += timedelta(days=1)
        return (tot / n) if n else 0.0
    if spec.get("mode") == "point":
        fix = _obs_date(start, lag, unit)
        return (_rate_at(idx, fix) if _realized(idx, fix, calc_date) else fwd_pct(fix)) or 0.0
    if spec.get("mode") == "month_start":
        # фиксинг на 1-е число месяца, на который приходится старт периода
        # (ИЖА ДОМ.РФ): ставка известна с начала месяца, лаг не участвует
        fix = start.replace(day=1)
        return (_rate_at(idx, fix) if _realized(idx, fix, calc_date) else fwd_pct(fix)) or 0.0
    if spec.get("mode") == "avg_prev":
        # среднее индекса по ПРЕДЫДУЩЕМУ периоду со сдвигом lag назад от старта:
        # окно [start − period − lag, start − lag), period = длина текущего периода.
        # Ставка ПОЛНОСТЬЮ известна на старте (окно целиком в прошлом при start ≤
        # calc+lag) — точная реконструкция конвенции «RUONIAср за T-(period+lag)..T-lag»
        # (Русагро, ОФЗ-ПК Минфина, РСХБ). Без аппроксимации точечным фиксингом.
        period = (end - start).days
        w_hi = _obs_date(start, lag, unit)
        w_lo = w_hi - timedelta(days=period)
        tot, n, cur = 0.0, 0, w_lo
        while cur < w_hi:
            k = _rate_at(idx, cur) if _realized(idx, cur, calc_date) else fwd_pct(cur)
            if k is not None:
                tot += k
                n += 1
            cur += timedelta(days=1)
        return (tot / n) if n else 0.0
    # ДНИ ДОХОДА = (start, end]: НКД-конвенция — день старта не начисляется,
    # день выплаты начисляется («Dji — дата, на которую рассчитывается доход»).
    # Раньше цикл шёл по [start, end): те же N дней, но все obs-даты сдвинуты
    # на −1 → эффективный лаг на день больше проспектного. Сверка по копейкам
    # с эмитентом (БалтЛизП10, 10 купонов): (s, e] совпадает точно, [s, e)
    # врал на копейку в 7 из 10. Калибратор компенсировал сдвиг фитом lag−1,
    # но парсер-лаг (авторитетный) перебивал его и тянул ошибку в прайсинг.
    tot, n, cur = 0.0, 0, start + timedelta(days=1)
    while cur <= end:
        obs = _obs_date(cur, lag, unit)
        k = _rate_at(idx, obs) if _realized(idx, obs, calc_date) else fwd_pct(obs)
        if k is not None:
            tot += k
            n += 1
        cur += timedelta(days=1)
    return (tot / n) if n else 0.0
