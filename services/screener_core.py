"""Ядро скринера: описание фильтра и его прогон по рынку. Владелец один —
вкладка СИГНАЛЫ (services/signals.py); доставка ветвится на браузер (WS) и
привязанный телеграм-чат (services/tg_notify.py), условия и цифры общие.

Фильтр = отбор бумаг + условия сделки:
  отбор — три селектора (рейтинги / эмитенты / ISIN), объединяются по ИЛИ;
          пусто во всех трёх = весь рынок;
  условия — сторона стакана, диапазон Y-IDX и деньги на этой стороне; всегда И.

Ничего не считает сам: метрики берутся из market_cache['universe_metrics']
(движок universe_stream), лестницы — из services.depth."""
import logging
import os
import re
import time
from datetime import date
from typing import Dict, Iterable, List, Optional

from services.ratings import rating_to_bucket

logger = logging.getLogger(__name__)

# Селектор работает КРУПНОЙ шкалой (грейды). Ступени (+/−) из реестра
# схлопываются rating_to_bucket — «AA» выбирает и AA, и AA+, и AA−.
RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"]

# Субординация — по названию бумаги: отдельного признака в реестре нет (ни MOEX,
# ни corpbonds его не отдают), а маркер в short_name — устойчивая конвенция
# («ВТБСУБ1-12», «ВТБСУБТ1Р2»). Ловим и добавочный капитал (Т1/T1, перп).
_SUBORD_RE = re.compile(r"СУБ|SUB|ПЕРП|PERP|(?<![A-ZА-Я0-9])[TТ]1(?![0-9])", re.I)

# Эмитент государства vs корпорат: отдельного признака в реестре нет (у ОФЗ
# MOEX не отдаёт EMITTER_ID вовсе), поэтому опознаём по трём уликам —
# SECID ленты (SU26248RMFS3), синтетическому имени эмитента из реестра
# («Минфин России») и названию выпуска («ОФЗ 29014»).
ISSUERS = ("all", "ofz", "corp")
_OFZ_NAME_RE = re.compile(r"^\s*(ОФЗ|SU\d)", re.I)

PARAM_DEFAULTS = {
    "ratings": [],          # ['AAA','AA'] — ИЛИ
    "emitters": [],         # ['Газпром капитал'] — ИЛИ, точное имя из реестра
    "isins": [],            # ['RU000A10AU99'] — ИЛИ
    "issuer": "all",        # all | ofz (только ОФЗ) | corp (без ОФЗ)
    "side": "ask",          # 'ask' — оффер (можно купить) | 'bid' — бид (продать)
    "spread_min": None,     # Y-IDX бп, нижняя граница диапазона
    "spread_max": None,     # Y-IDX бп, верхняя граница
    "min_money_rub": None,  # деньги на выбранной стороне стакана, руб
    "money_mode": "book",   # 'book' — набрать сумму по лестнице (VWAP на тикет)
                            # 'single' — ОДНА заявка не меньше суммы (крупный принт)
    "years_min": None,      # лет до погашения, нижняя граница
    "years_max": None,      # лет до погашения, верхняя граница
    "hide_subord": False,   # прятать суборды (см. _SUBORD_RE)
    # ПОВТОРНОЕ срабатывание по уже найденной бумаге. Спред — всегда, объём —
    # по желанию: стакан по ликвидной бумаге дышит объёмом постоянно, и для
    # того, кто следит за уровнем спреда, это шум. Первое попадание («заявка»)
    # приходит в любом случае — это не повтор.
    "repeat_on_money": True,
}

MAX_SELECTOR_ITEMS = 50


class FilterError(ValueError):
    pass


def _str_list(raw, field: str, upper: bool = False) -> list:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise FilterError(f"{field}: ожидался список")
    out = []
    for v in raw:
        v = str(v or "").strip()
        if not v:
            continue
        out.append(v.upper() if upper else v)
    if len(out) > MAX_SELECTOR_ITEMS:
        raise FilterError(f"{field}: не больше {MAX_SELECTOR_ITEMS} значений")
    return out


def _issuer(raw) -> str:
    v = (raw or "all").strip().lower()
    if v not in ISSUERS:
        raise FilterError("issuer: " + " | ".join(ISSUERS))
    return v


def _years_bounds(raw: dict, p: dict) -> None:
    """Срок до погашения: общая валидация book- и block-фильтра."""
    for k in ("years_min", "years_max"):
        if p[k] is not None and p[k] < 0:
            raise FilterError("Срок до погашения: неотрицательное число")
    if (p["years_min"] is not None and p["years_max"] is not None
            and p["years_min"] > p["years_max"]):
        raise FilterError("Срок до погашения: «от» больше «до»")


def _floats(raw: dict, p: dict, keys) -> None:
    for k in keys:
        v = raw.get(k)
        if v is None or v == "":
            continue
        try:
            p[k] = float(v)
        except (TypeError, ValueError):
            raise FilterError(f"{k}: должно быть числом")


def is_ofz(u: dict) -> bool:
    """Выпуск Минфина. Улики берём по очереди: SECID ленты (у ОФЗ он SU…),
    имя эмитента из реестра, название выпуска."""
    if (u.get("secid") or "").strip().upper().startswith("SU"):
        return True
    # Именно федеральный Минфин: субфедералы в реестре тоже идут «Минфин
    # <области>» («Амур 24001» — Минфин Амурской обл.), а это корпоративный
    # по спреду выпуск, не ОФЗ.
    if (u.get("emitter_name") or "").strip().upper() == "МИНФИН РОССИИ":
        return True
    return bool(_OFZ_NAME_RE.match(u.get("name") or ""))


def issuer_ok(u: dict, params: dict) -> bool:
    who = params.get("issuer") or "all"
    if who == "all":
        return True
    return is_ofz(u) if who == "ofz" else not is_ofz(u)


def normalize_params(raw: dict) -> dict:
    raw = raw or {}
    p = dict(PARAM_DEFAULTS)
    p["ratings"] = _str_list(raw.get("ratings"), "ratings", upper=True)
    p["emitters"] = _str_list(raw.get("emitters"), "emitters")
    p["isins"] = _str_list(raw.get("isins"), "isins", upper=True)
    for r in p["ratings"]:
        if r not in RATINGS:
            raise FilterError(f"rating: {' '.join(RATINGS)}")
    p["side"] = raw.get("side") or "ask"
    if p["side"] not in ("ask", "bid"):
        raise FilterError("side: ask | bid")
    p["issuer"] = _issuer(raw.get("issuer"))
    p["hide_subord"] = bool(raw.get("hide_subord"))
    p["repeat_on_money"] = (True if raw.get("repeat_on_money") is None
                            else bool(raw.get("repeat_on_money")))
    _floats(raw, p, ("spread_min", "spread_max", "min_money_rub",
                     "years_min", "years_max"))
    if p["min_money_rub"] is not None and p["min_money_rub"] <= 0:
        raise FilterError("min_money_rub: положительное число")
    p["money_mode"] = raw.get("money_mode") or "book"
    if p["money_mode"] not in ("book", "single"):
        raise FilterError("money_mode: book | single")
    if (p["spread_min"] is not None and p["spread_max"] is not None
            and p["spread_min"] > p["spread_max"]):
        raise FilterError("Диапазон спреда: «от» больше «до»")
    # Спред НЕобязателен: «покажи крупные заявки в ААА» — законный фильтр без
    # единого слова про спред. Но совсем пустых условий быть не должно, иначе
    # сигнал сведётся к «уведомляй обо всём рынке».
    if (p["spread_min"] is None and p["spread_max"] is None
            and p["min_money_rub"] is None):
        raise FilterError("Задай границу спреда или объём — иначе условий нет")
    _years_bounds(raw, p)
    return p


# ────────────────────── фильтр «крупная сделка» (kind=block) ──────────────────
#
# Другой класс события: не состояние стакана, а ФАКТ сделки в ленте. Поэтому и
# параметры другие — порог в рублях, режим торгов (безадресный / РПС) и база
# купона; спреда/стороны стакана здесь нет. Отбор бумаг (рейтинг/эмитент/ISIN,
# суборды) общий с book-фильтром, чтобы «крупняк только в моих эмитентах»
# описывался теми же чипами.
BLOCK_PARAM_DEFAULTS = {
    "ratings": [],
    "emitters": [],
    "isins": [],
    "issuer": "all",              # all | ofz | corp — как в фильтре стакана
    "bases": [],                  # KEYRATE/RUONIA/FIXED, пусто = любая база
    "min_value_rub": 100_000_000.0,
    "markets": "all",             # all | main (безадресные) | ndm (РПС/адресные)
    "side": "any",                # any | buy | sell — агрессор сделки
    # Спред сделки (Y-IDX, бп) и срок до погашения — те же условия, что у
    # фильтра стакана. Спред известен только флоатерам (солвер считает их), так
    # что заданный диапазон сам по себе отсекает фиксы.
    "spread_min": None,
    "spread_max": None,
    "years_min": None,
    "years_max": None,
    "hide_subord": False,
}

BLOCK_BASES = ["KEYRATE", "RUONIA", "FIXED"]


def normalize_block_params(raw: dict) -> dict:
    raw = raw or {}
    p = dict(BLOCK_PARAM_DEFAULTS)
    p["ratings"] = _str_list(raw.get("ratings"), "ratings", upper=True)
    p["emitters"] = _str_list(raw.get("emitters"), "emitters")
    p["isins"] = _str_list(raw.get("isins"), "isins", upper=True)
    p["bases"] = _str_list(raw.get("bases"), "bases", upper=True)
    for r in p["ratings"]:
        if r not in RATINGS:
            raise FilterError(f"rating: {' '.join(RATINGS)}")
    for b in p["bases"]:
        if b not in BLOCK_BASES:
            raise FilterError(f"base: {' '.join(BLOCK_BASES)}")
    v = raw.get("min_value_rub")
    if v in (None, ""):
        raise FilterError("Задай порог объёма сделки")
    try:
        p["min_value_rub"] = float(v)
    except (TypeError, ValueError):
        raise FilterError("min_value_rub: должно быть числом")
    # Нижняя граница — порог ЗАПИСИ ленты: сделок мельче в базе может не быть
    # вовсе (ночью день ужимается), обещать по ним звонок нечестно.
    if p["min_value_rub"] < 1_000_000:
        raise FilterError("Порог объёма: от 1 млн ₽")
    p["markets"] = raw.get("markets") or "all"
    if p["markets"] not in ("all", "main", "ndm"):
        raise FilterError("markets: all | main | ndm")
    p["side"] = raw.get("side") or "any"
    if p["side"] not in ("any", "buy", "sell"):
        raise FilterError("side: any | buy | sell")
    p["issuer"] = _issuer(raw.get("issuer"))
    _floats(raw, p, ("spread_min", "spread_max", "years_min", "years_max"))
    if (p["spread_min"] is not None and p["spread_max"] is not None
            and p["spread_min"] > p["spread_max"]):
        raise FilterError("Диапазон спреда: «от» больше «до»")
    _years_bounds(raw, p)
    p["hide_subord"] = bool(raw.get("hide_subord"))
    p["repeat_on_money"] = (True if raw.get("repeat_on_money") is None
                            else bool(raw.get("repeat_on_money")))
    return p


def block_matches(trade: dict, meta: dict, params: dict,
                  today: Optional[date] = None) -> bool:
    """Подходит ли сделка из ленты под условия block-фильтра.

    trade — строка block_trade (value/market/side/isin/secid/y_idx_bps), meta —
    подпись бумаги из instruments_registry.labels_map: {name, emitter, base,
    rating, maturity}."""
    if (trade.get("value") or 0) < money_floor(params["min_value_rub"]):
        return False
    if params["markets"] == "ndm" and trade.get("market") != "ndm":
        return False
    if params["markets"] == "main" and trade.get("market") == "ndm":
        return False
    if params["side"] != "any" and trade.get("side") != params["side"]:
        return False
    base = (meta.get("base") or "FIXED").upper()
    if params["bases"]:
        # всё, что не флоатер, для фильтра — FIXED: своих подтипов у фиксов в
        # реестре нет, а «крупняк в фиксах» пользователь описывает одной кнопкой
        if (base if base in ("KEYRATE", "RUONIA") else "FIXED") not in params["bases"]:
            return False
    if params["hide_subord"] and is_subord({"name": meta.get("name")}):
        return False
    u = {"isin": trade.get("isin"), "secid": trade.get("secid"),
         "name": meta.get("name"), "rating": meta.get("rating"),
         "emitter_name": meta.get("emitter")}
    if not issuer_ok(u, params):
        return False
    lo, hi = params.get("spread_min"), params.get("spread_max")
    if lo is not None or hi is not None:
        # Спред считается только флоатерам и только сделкам крупнее
        # BLOCK_YIDX_MIN_RUB: непосчитанный спред — это НЕ «подходит».
        val = trade.get("y_idx_bps")
        if val is None:
            return False
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            return False
    # срок — до горизонта прайсинга сделки, если он известен ленте, иначе до
    # погашения (у ленты есть только справочная метка бумаги)
    if not years_ok(meta_years(meta, today or date.today()), params):
        return False
    return selected(u, params)


def is_subord(u: dict) -> bool:
    return bool(_SUBORD_RE.search(u.get("name") or ""))


def years_left(maturity_iso: Optional[str], today: date) -> Optional[float]:
    """Лет до даты, ACT/365.25 (календарные годы — это СРОК, не duration).

    Берём первые 10 символов: часть источников отдаёт дату со временем
    («2027-04-08 00:00:00»), а date.fromisoformat такую строку не принимает —
    без среза бумага молча выпадала бы из окна срока."""
    try:
        return (date.fromisoformat(str(maturity_iso)[:10]) - today).days / 365.25
    except (TypeError, ValueError):
        return None


def horizon_years(u: dict, row: Optional[dict], today: date) -> Optional[float]:
    """Срок бумаги в годах до ГОРИЗОНТА ПРАЙСИНГА — той даты, к которой посчитан
    её спред (оферта/колл по правилу цены, иначе погашение).

    Одна методика срока на весь проект: витрина фильтрует окно срока ровно так
    (App.jsx, hzDate). Пока скринер, портфель и лента считали срок только до
    ПОГАШЕНИЯ, один и тот же фильтр «до 2 лет» в МОНИТОРЕ и в СИГНАЛАХ давал
    разные множества: бумага с путом через полгода и спредом к этому путу в
    сигналы не попадала.

    row — строка live-метрик (universe.enrich_bond) или None: горизонт зависит
    от цены, статически он неизвестен, и тогда остаётся погашение."""
    d = None
    if row and (row.get("horizon") in ("put", "call")) and row.get("offer_date"):
        d = row["offer_date"]
        d = d.isoformat() if hasattr(d, "isoformat") else str(d)
    return years_left(d or u.get("maturity_date"), today)


def block_meta(labels: dict, isin: str, metrics: Optional[dict] = None) -> dict:
    """Справочная метка бумаги для ленты сделок + ГОРИЗОНТ ПРАЙСИНГА из живых
    метрик. Без горизонта фильтр ленты мерил срок только до погашения, а тот же
    фильтр в мониторе — до оферты: одна бумага, два разных «срока»."""
    meta = dict(labels.get(isin) or {})
    row = (metrics or {}).get(isin) or {}
    if row:
        meta["horizon"] = row.get("horizon")
        meta["offer_date"] = row.get("offer_date")
    return meta


def meta_years(meta: dict, today: date) -> Optional[float]:
    """Срок бумаги ленты до её горизонта прайсинга (одна методика с монитором)."""
    return horizon_years({"maturity_date": meta.get("maturity")}, meta, today)


def years_ok(yrs: Optional[float], params: dict) -> bool:
    """Окно срока фильтра. Без даты (перп/дыра в справочнике) при заданной
    границе — не пускаем: иначе «до 2 лет» молча притащит бессрочную."""
    ylo, yhi = params.get("years_min"), params.get("years_max")
    if ylo is None and yhi is None:
        return True
    if yrs is None:
        return False
    return not ((ylo is not None and yrs < ylo) or (yhi is not None and yrs > yhi))


def selected(u: dict, params: dict) -> bool:
    """Отбор бумаги селекторами: рейтинг ИЛИ эмитент ИЛИ ISIN. Ни одного
    селектора не задано → весь рынок."""
    sel_r, sel_e, sel_i = params["ratings"], params["emitters"], params["isins"]
    if not (sel_r or sel_e or sel_i):
        return True
    # По ГРЕЙДУ, а не по строке: в реестре у бумаги может стоять ступень
    # («AA-»), а селектор сигналов/бота работает крупной шкалой (RATINGS) —
    # точное сравнение молча выкидывало такие бумаги из отбора.
    if sel_r and rating_to_bucket(u.get("rating")) in sel_r:
        return True
    if sel_e and (u.get("emitter_name") or "").strip() in sel_e:
        return True
    if sel_i and (u.get("isin") or "").strip().upper() in sel_i:
        return True
    return False


def side_money_rub(ladder: Optional[dict], side: str, face: float,
                   accrued: float = 0.0) -> Optional[float]:
    """Σ руб по выбранной стороне снимка глубины {'a'|'b': [[px_pct, qty], ...]}.
    Деньги ГРЯЗНЫЕ — реальная сумма расчётов, как в frontend/src/vwap.js."""
    if not ladder:
        return None
    total = 0.0
    for lvl in (ladder.get("a" if side == "ask" else "b") or []):
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        total += level_money(px, qty, face, accrued)
    return total or None


# --- VWAP на объём. Порт frontend-react/src/vwap.js: фильтр по объёму в таблице
# и сигналы обязаны давать одну и ту же цену на один и тот же тикет. Правила
# оттуда же: деньги уровня грязные, последний уровень берётся частично, набор
# засчитывается при добранных ≥ VOL_TOL от запрошенного. ---

VOL_TOL = 0.9


def money_floor(want: Optional[float]) -> Optional[float]:
    """Порог объёма с люфтом: сколько денег ДЕЙСТВИТЕЛЬНО отсекает «от 50 млн».

    Человек, ставя порог, называет ПОРЯДОК, а не границу с точностью до рубля:
    сделка на 48 млн под фильтром «от 50» — ровно то, ради чего фильтр заведён,
    а жёсткое сравнение выбрасывало её и из алертов, и из таблицы. Тот же
    VOL_TOL, что уже применялся к неполному набору по лестнице (vwap_passes) —
    правило одно на все пороги объёма, чтобы витрина, лента и сигналы не
    расходились в том, что считается попаданием.

    Возвращает None для пустого порога (фильтра по объёму нет) — вызывающему
    остаётся передать результат туда же, где стоял сырой порог."""
    return None if not want else float(want) * VOL_TOL


def level_money(px_pct: Optional[float], qty: Optional[float], face: float,
                accrued: float = 0.0) -> float:
    if px_pct is None or qty is None or not face:
        return 0.0
    return qty * (face * px_pct / 100.0 + (accrued or 0.0))


def vwap_for(levels, want_rub: float, face: float,
             accrued: float = 0.0) -> Optional[dict]:
    """Средневзвешенная цена набора want_rub рублей по лестнице (от лучшей цены).
    → {px, money, last_px, levels, partial} либо None. partial=True — глубины не
    хватило, px посчитан по всей книге.

    money — сколько НАБРАЛИ (≈ want_rub, наш лимит). last_px — цена ПОСЛЕДНЕГО
    задействованного уровня, то есть граница, до которой дошёл набор: по ней и
    меряется накопленный объём (money_upto). Средневзвешенная px для этого не
    годится — она всегда ЛУЧШЕ худшего взятого уровня, и накопление по ней
    выходило меньше самого набора (Газпн3P14R 24.08: набрали 1 млн, а в шапке
    стояло 250,5к, потому что 3 из 7 уровней оказались дороже средневзвеса)."""
    if not levels or not (want_rub > 0) or not face:
        return None
    left, cost, taken, used, last = float(want_rub), 0.0, 0.0, 0, None
    for lvl in levels:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        money = level_money(px, qty, face, accrued)
        if money <= 0:
            continue
        used += 1
        last = px
        part = min(money, left)
        cost += part * px           # цену взвешиваем деньгами, не количеством
        taken += part
        left -= part
        if left <= 1e-9:
            break
    if taken <= 0:
        return None
    return {"px": cost / taken, "money": taken, "last_px": last,
            "levels": used, "partial": left > 1e-9}


def money_upto(levels, px: Optional[float], side: str, face: float,
               accrued: float = 0.0) -> Optional[float]:
    """НАКОПЛЕННЫЙ объём до цены px: сколько денег стоит по цене не хуже неё —
    для оффера это уровни дешевле-или-равно, для бида дороже-или-равно.

    Именно это число человек и называет «объёмом по 99,86»: не наш набранный
    лимит (он равен порогу фильтра) и не сумма всей стороны книги, а вся
    глубина до цены, куда дошёл набор."""
    if px is None or not levels or not face:
        return None
    tot = 0.0
    for lvl in levels:
        try:
            p, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if side == "ask" and p > px + 1e-9:
            continue
        if side == "bid" and p < px - 1e-9:
            continue
        m = level_money(p, qty, face, accrued)
        if m > 0:
            tot += m
    return tot or None


def best_level(levels, face: float, accrued: float = 0.0) -> Optional[dict]:
    """Самый «денежный» уровень стороны → {price, qty, money}. Для режима
    крупной заявки: интересует не сумма книги, а одна большая заявка."""
    best = None
    for lvl in (levels or []):
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        money = level_money(px, qty, face, accrued)
        if money <= 0:
            continue
        if best is None or money > best["money"]:
            best = {"price": px, "qty": qty, "money": money}
    return best


def vwap_passes(v: Optional[dict], want_rub: float) -> bool:
    if not v:
        return False
    return (not v["partial"]) or v["money"] >= want_rub * VOL_TOL


def book_snapshot(depth_side: Optional[dict], row: dict, face: float,
                  accrued: float = 0.0, levels: int = 4,
                  isin: Optional[str] = None) -> dict:
    """Лестница стакана НА МОМЕНТ СОБЫТИЯ: по levels уровней с каждой стороны,
    у каждого — цена, деньги и Y-IDX.

    Снимок делается там же, где сработал фильтр, из того же depth_map: пока
    уведомление доедет до телефона, книга уже поменяется, а вопрос «что там
    вообще стояло» — первый после самого сигнала.

    Y-IDX уровня считается ТЕМ ЖЕ путём, что число в шапке события — батчем по
    методике на тёплом контексте. Раньше здесь стоял наклон y_idx_at, и
    лестница жила своей арифметикой: 21.08.2026 РусГид2Р01 приехал с 181 bps в
    шапке и 103 на том же уровне в стакане."""
    d = depth_side or {}
    pxs = [_px(l) for key in ("a", "b") for l in (d.get(key) or [])[:levels]]
    exact_map = exact_y_idx_map(isin, pxs)

    def level_y(px: float, side: str) -> Optional[float]:
        # без контекста/НКД числа нет — прочерк честнее наклона от чужого якоря
        return exact_map.get(round(float(px), _EXACT_PX_DIGITS))

    def side_rows(key: str, best_first: bool) -> list:
        out = []
        for lvl in (d.get(key) or [])[:levels]:
            px, qty = _px(lvl), _qty(lvl)
            if px is None or qty is None:
                continue
            out.append({"price": px, "qty": qty,
                        "money": level_money(px, qty, face, accrued),
                        "y_idx": level_y(px, "ask" if key == "a" else "bid")})
        return out if best_first else list(reversed(out))

    return {"asks": side_rows("a", False), "bids": side_rows("b", True)}


def money_in_spread(levels, row: dict, side: str, lo: Optional[float],
                    hi: Optional[float], face: float,
                    accrued: float = 0.0,
                    isin: Optional[str] = None) -> Optional[float]:
    """Σ руб на стороне ПО НАШИМ УСЛОВИЯМ: только уровни, чей Y-IDX попадает в
    диапазон спреда фильтра.

    Это метрика повторного сигнала «объём изменился»: сумма всей стороны шумит
    дальними уровнями, по которым никто торговать не станет, а набор VWAP на
    тикет всегда равен запрошенному лимиту (и потому не меняется вовсе).
    Здесь же видно ровно то, что трейдер и называет объёмом: сколько денег
    стоит по цене, которая нас устраивает. Границы не заданы (фильтр «крупные
    заявки») — считаем всю сторону."""
    if not levels:
        return None
    if lo is None and hi is None:
        total = sum(level_money(_px(l), _qty(l), face, accrued) for l in levels)
        return total or None
    # Спред КАЖДОГО уровня — по методике, одним батчем (см. exact_y_idx_map).
    # Наклон убран 27.08.2026: он отбирал уровни по числам, уехавшим вслед за
    # якорем, и метрика повторного сигнала срабатывала не на том объёме.
    exact_map = exact_y_idx_map(isin, [_px(l) for l in levels])
    total = 0.0
    for lvl in levels:
        px = _px(lvl)
        val = None if px is None else exact_map.get(round(float(px), _EXACT_PX_DIGITS))
        if val is None:
            continue
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        total += level_money(px, _qty(lvl), face, accrued)
    return total or None


def _px(lvl) -> Optional[float]:
    try:
        return float(lvl[0])
    except (TypeError, ValueError, IndexError):
        return None


def _qty(lvl) -> Optional[float]:
    try:
        return float(lvl[1])
    except (TypeError, ValueError, IndexError):
        return None


# ── ТОЧНЫЙ Y-IDX по цене (та же механика, что стакан и карточка) ───────────
#
# Наклонная оценка (y_idx_at) считает Y-IDX линейно от якоря — верха стакана.
# Формула верна, ломается ЯКОРЬ: bid/ask приходят из потока котировок
# (событийно), лестница стакана — из батч-снимка глубины (~раз в 120с). Пока
# один обновился, а второй нет, якорь и цена набора относятся к разным моментам.
# У короткой бумаги наклон достигает −450 bps на 1пп цены, и зазор в десятые
# доли пункта даёт сотни bps: РСетиМР1Р5 20.08.2026 — сигнал 378 bps на цене
# 100.34, где верный Y-IDX +5 (378 — это цена ≈99.5, куда уехал якорь).
#
# Поэтому число, которое видит пользователь, считается ВЕРИФИЦИРОВАННЫМ путём:
# reprice_at_price на тёплом контексте — тот же вызов, что per-level метрики
# стакана и калькулятор карточки, поэтому цифры сходятся поштучно.
# isin → (собран_в_monotonic, ctx, memo). ЖИВЁТ МИНУТЫ, НЕ ДЕНЬ: в контексте
# лежит снимок мира на момент сборки — кривая, НКД, расписание, номинал после
# амортизации. Дневной кэш означал, что один неудачный сбор (кэши ещё не
# прогреты после рестарта, расписание пришло без ставок будущих купонов)
# отравлял число до полуночи: РусГид2Р01 21.08.2026 звонил с 181 bps на цене
# 100,23, где верные 103 — и ровно те же 103 стояли в приложенном стакане,
# потому что его уровни считались другим путём.
# memo лежит ВНУТРИ записи: пересборка контекста обязана обнулять и его,
# иначе протухшее число переживало бы собственный контекст.
_exact_ctx: dict = {}
_EXACT_PX_DIGITS = 3
# Пересборка тёплой бумаги стоит ~58 мс (замер на проде 21.08.2026: всё из
# кэшей — кривые в памяти, расписание на диске), поэтому окно короткое.
EXACT_CTX_TTL_SEC = float(os.getenv("SIGNALS_EXACT_CTX_TTL_SEC", "600"))
# Потолок пересборок за тик: обновление размазывается по тактам, а не встаёт
# одним десятисекундным комом на всём наборе кандидатов.
EXACT_REWARM_PER_TICK = int(os.getenv("SIGNALS_EXACT_REWARM", "40"))


def _ctx_fresh(rec) -> bool:
    return bool(rec) and (time.monotonic() - rec[0]) < EXACT_CTX_TTL_SEC


async def warm_exact_ctx(isins) -> int:
    """Тёплые контексты пересчёта для бумаг-кандидатов.

    Бумага БЕЗ контекста греется вперёд протухшей: без числа она молчит вовсе,
    а протухшее хотя бы близко (и живёт максимум EXACT_CTX_TTL_SEC)."""
    from services.bond_details import load_reprice_ctx
    from services.market_data import MarketDataService
    from services.paths import cache_path

    cold, stale = [], []
    for i in isins:
        if not i:
            continue
        rec = _exact_ctx.get(i)
        if rec is None:
            cold.append(i)
        elif not _ctx_fresh(rec):
            stale.append(i)
    need = (cold + stale)[:EXACT_REWARM_PER_TICK]
    if not need:
        return 0
    cache = MarketDataService.get_local_bond_cache(cache_path("isins_cache.json"))
    done = failed = 0
    for isin in need:
        try:
            _exact_ctx[isin] = (time.monotonic(), await load_reprice_ctx(isin, cache), {})
            done += 1
        except Exception as e:
            # бумага без контекста просто не получит точного числа — фильтр
            # отсеет её по None, а не пропустит с кривой оценкой
            logger.debug("warm_exact_ctx %s: %s", isin, e)
            _exact_ctx[isin] = (time.monotonic(), None, {})
            failed += 1
    # массовый промах = фильтры молчат, и молчат ТИХО: без этой строки сигналы
    # выглядели бы как «рынок спокоен», хотя на деле отвалился источник
    if failed and failed >= max(3, len(need) // 2):
        logger.warning("warm_exact_ctx: контекст не собрался у %d из %d бумаг — "
                       "точный спред недоступен, события по ним не придут",
                       failed, len(need))
    return done


def _sync_ctx_curves(ctx: dict, memo: dict) -> None:
    """Кривая в контексте — ЖИВОЙ объект рынка, а не свойство бумаги.

    load_reprice_ctx кладёт в ctx ссылку на кривую из market_cache, и раньше
    эта ссылка замерзала вместе с контекстом. Пока котировки свежие, кривая
    пинится на день и это незаметно; но когда Cbonds отдаёт вчерашнее
    (rates_date < сегодня — прод 21.08.2026), market_data пересобирает её
    каждые ~15 минут, и контекст держал СВОЮ версию: РусГид2Р01 звонил 181 bps
    там, где текущая кривая давала 103 (доходность 15,79% одна и та же —
    расходился индекс: 13,98% против 14,76%).

    Поэтому перед каждым расчётом берём кривые из кэша заново. Сменились —
    запомненные числа обнуляем: они считались на прошлой кривой."""
    from services.market_data import market_cache
    ru = market_cache.get("ruonia_curve")
    ks = market_cache.get("keyrate_curve")
    base = getattr(ctx.get("ref_obj"), "base", None)
    live = ru if base == "RUONIA" else ks
    changed = False
    if live is not None and live is not ctx.get("curve"):
        ctx["curve"] = live
        changed = True
    if ru is not None and ru is not ctx.get("ruonia_curve"):
        ctx["ruonia_curve"] = ru
        changed = True
    if changed:
        memo.clear()


def exact_y_idx(isin: str, px: Optional[float]) -> Optional[float]:
    """Y-IDX по цене ТОЧНО: reprice_at_price на горизонте по правилу цены —
    один в один с уровнем стакана. None — контекст не прогрет/протух/расчёт
    не сошёлся (протухший НЕ используем: тихое старое число хуже молчания)."""
    if px is None or not isin:
        return None
    rec = _exact_ctx.get(isin)
    if not _ctx_fresh(rec) or rec[1] is None:
        return None
    ctx, memo = rec[1], rec[2]
    if ctx.get("accrued_missing"):
        # НКД неизвестен — «точного» числа не бывает: расчёт начислит своё, и
        # спред уедет на десятки б.п. (прод 27.08.2026, см. load_reprice_ctx).
        # Потребитель откатится на наклон от строки метрик, где НКД биржевой.
        return None
    _sync_ctx_curves(ctx, memo)
    key = round(float(px), _EXACT_PX_DIGITS)
    if key in memo:
        return memo[key]
    try:
        from services.bond_details import reprice_at_price
        from services.valuation import pick_horizon
        m = reprice_at_price(ctx, float(px))
        h = pick_horizon(m, "auto")
        val = h.get("yield_over_index_bps", m.get("yield_over_index_bps"))
    except Exception as e:
        logger.debug("exact_y_idx %s@%s: %s", isin, px, e)
        val = None
    memo[key] = val
    return val


def exact_y_idx_map(isin: Optional[str],
                    prices: Iterable[Optional[float]]) -> Dict[float, Optional[float]]:
    """{цена: Y-IDX} для НАБОРА цен одной бумаги — одним расчётом по методике.

    Лестница стакана и отбор уровней по спреду просят десяток цен разом. Поштучный
    exact_y_idx стоил бы 85 мс на цену, батч через alt_prices — 1,5 мс (замер на
    проде 27.08.2026), поэтому цены считаются пачкой, а не по одной.

    Контекст не прогрет, протух или без биржевого НКД — карта пустая: наклон от
    чужого якоря увёл бы все уровни разом (ровно это и случилось 27.08).
    Посчитанное кладём в тот же memo, что exact_y_idx, — один кэш на бумагу.
    """
    keys = []
    for p in prices or []:
        if p is None:
            continue
        k = round(float(p), _EXACT_PX_DIGITS)
        if k > 0 and k not in keys:
            keys.append(k)
    if not keys or not isin:
        return {}
    rec = _exact_ctx.get(isin)
    if not _ctx_fresh(rec) or rec[1] is None:
        return {}
    ctx, memo = rec[1], rec[2]
    _sync_ctx_curves(ctx, memo)          # кривая сменилась → memo уже очищен
    out = {k: memo[k] for k in keys if k in memo}
    todo = [k for k in keys if k not in memo]
    if todo:
        from services.yidx_exact import y_idx_many
        got = y_idx_many(ctx, todo)
        for k in todo:
            # цены, которых расчёт не дал, тоже запоминаем: повторять батч по
            # ним в каждом такте — та же работа с тем же результатом
            memo[k] = out[k] = got.get(k)
    return out


def drop_exact_cache(isin: Optional[str] = None) -> None:
    """Сброс тёплых контекстов: правка параметров бумаги в Справочнике меняет
    поток, а значит и Y-IDX любой цены."""
    if isin:
        _exact_ctx.pop(isin, None)
    else:
        _exact_ctx.clear()


def y_idx_at(row: dict, px: Optional[float], side: str) -> Optional[float]:
    """Y-IDX по произвольной цене ПРИБЛИЖЁННО: линейно от якоря через наклон
    dY/dP. Держится только рядом с якорем и только пока якорь свеж — наружу
    такое число не отдаём (см. exact_y_idx), это грубый предфильтр."""
    k = row.get("yoi_slope")
    if px is None or k is None:
        return None
    if side == "ask":
        anchors = [(row.get("ask"), row.get("yoi_ask")), (row.get("bid"), row.get("yoi_bid")),
                   (row.get("last"), row.get("yoi"))]
    else:
        anchors = [(row.get("bid"), row.get("yoi_bid")), (row.get("ask"), row.get("yoi_ask")),
                   (row.get("last"), row.get("yoi"))]
    for ap, ay in anchors:
        if ap is not None and ay is not None:
            return ay + (px - ap) * k
    return None


def y_idx_diag(isin: str, px: Optional[float], row: dict, side: str) -> dict:
    """ИЗ ЧЕГО сложилось число события: точный путь, наклон, индекс-база,
    возраст контекста и якоря строки метрик.

    Диагностика поставлена 2026-08-27: на проде спред одной бумаги прыгал
    188↔166 бп при неизменных цене и книге, а в отдельном процессе оба движка
    стабильно дают 188. Значит расходится состояние ЖИВОГО воркера, и увидеть
    его можно только изнутри него."""
    rec = _exact_ctx.get(isin)
    ctx = rec[1] if rec and len(rec) > 1 else None
    out = {
        "exact": exact_y_idx(isin, px),
        "slope": y_idx_at(row, px, side),
        "ctx_age_s": None if not rec else round(time.monotonic() - rec[0], 1),
        "ask": row.get("ask"), "yoi_ask": row.get("yoi_ask"),
        "last": row.get("last"), "yoi": row.get("yoi"),
        "slope_bps_pp": row.get("yoi_slope"),
        "accrued": row.get("accrued_settle"), "face": row.get("face_px"),
    }
    if ctx is not None and px is not None:
        try:
            from services.bond_details import reprice_at_price
            from services.valuation import pick_horizon
            h = pick_horizon(reprice_at_price(ctx, float(px)), "auto")
            out.update(index_pct=h.get("index_yield_pct"), ytm=h.get("yield_xirr_pct"),
                       horizon=h.get("horizon"))
        except Exception as e:
            out["ctx_err"] = str(e)[:80]
        from services.market_data import market_cache
        out["curve_is_live"] = ctx.get("curve") is (
            market_cache.get("ruonia_curve")
            if getattr(ctx.get("ref_obj"), "base", None) == "RUONIA"
            else market_cache.get("keyrate_curve"))
        out["idx_curve_is_live"] = ctx.get("ruonia_curve") is market_cache.get("ruonia_curve")
        out["ctx_accrued"] = ctx.get("accrued_live")
        out["ctx_calc_date"] = str(ctx.get("calc_date"))
    return out


def static_candidates(params: dict, uni: List[dict],
                      today: Optional[date] = None,
                      metrics: Optional[dict] = None) -> List[dict]:
    """Отбор по НЕподвижным признакам: рейтинг/эмитент/ISIN, ОФЗ/корп, срок, суборд.
    Считается редко — множество меняется только с реестром, а не с рынком.
    Дальше мониторится только глубина этих бумаг (см. evaluate_candidates).

    metrics — снимок live-метрик: из него берётся горизонт прайсинга для срока
    (см. horizon_years). Без него срок считается до погашения, а окончательную
    проверку сроком делает evaluate_candidates уже по свежей строке."""
    today = today or date.today()
    hide_sub = params.get("hide_subord")
    out = []
    for u in uni:
        if hide_sub and is_subord(u):
            continue
        if not issuer_ok(u, params):
            continue
        yrs = horizon_years(u, (metrics or {}).get(u.get("isin")), today)
        if not years_ok(yrs, params):
            continue
        if not selected(u, params):
            continue
        out.append(dict(u, _years=round(yrs, 2) if yrs is not None else None))
    return out


def _price_y_idx(isin: str, row: dict, px: Optional[float], side: str,
                 exact: bool) -> Optional[float]:
    """Y-IDX цены для скринера. exact — верифицированный путь (как стакан), с
    откатом на наклон, если контекст бумаги не прогрет: без отката бумага молча
    исчезала бы из фильтра на первом тике после рестарта."""
    if exact:
        # ТОЛЬКО точное число. Откат на наклон/верх стакана здесь был бы возвратом
        # ровно к тому, что врало: top_val — это yoi_ask из снимка метрик, тот же
        # протухающий якорь (РСетиМР1P7 21.08: 166 bps в ленте против верных 28 —
        # набор уложился в один уровень и подставился top_val). Нет контекста —
        # нет числа: бумага молчит, а не сообщает выдумку.
        v = exact_y_idx(isin, px)
        return None if v is None else float(v)
    return y_idx_at(row, px, side)


def evaluate_candidates(params: dict, candidates: List[dict], metrics: dict,
                        depth_map: dict, exact: bool = False,
                        today: Optional[date] = None) -> List[dict]:
    """Рыночная часть: по уже отобранным бумагам считает цену/спред/деньги и
    отсеивает по диапазону спреда и объёму.

    Если задан min_money_rub — цена это СРЕДНЕВЗВЕС набора этого объёма по
    лестнице, а спред пересчитан к ней (иначе цифра относилась бы к верху
    стакана на 50 бумаг, а исполняться сделка будет по всей лестнице).
    Без объёма — верх стакана, как в таблице."""
    side = params["side"]
    lo, hi = params["spread_min"], params["spread_max"]
    want = params.get("min_money_rub")
    today = today or date.today()
    out = []
    for u in candidates:
        isin = u.get("isin")
        row = metrics.get(isin)
        if not row:
            continue
        if row.get("implausible") or row.get("price_stale") or row.get("price_thin"):
            continue
        # СРОК — по свежей строке: горизонт прайсинга зависит от цены и может
        # смениться внутри дня, а множество кандидатов кешируется на фильтр
        # (signals._candidates) и такую смену не увидит до обновления универса.
        if not years_ok(horizon_years(u, row, today), params):
            continue
        face = row.get("face_px") or 1000.0
        accrued = row.get("accrued_settle") or 0.0
        ladder = (depth_map.get(isin) or {}).get("a" if side == "ask" else "b")

        top_val = row.get("yoi_ask") if side == "ask" else row.get("yoi_bid")
        single_px = None
        if want and params.get("money_mode") == "single":
            # Крупная заявка: ищем САМЫЙ денежный уровень стороны. Набор по
            # лестнице тут не годится — двадцать мелких заявок на 5 млн не то
            # же самое, что одна заявка на 5 млн.
            best = best_level(ladder, face, accrued)
            if not best or best["money"] < money_floor(want):
                continue
            price = single_px = depth_px = best["price"]
            val = _price_y_idx(isin, row, price, side, exact)
            if val is None and not exact and price == row.get(side):
                val = top_val          # заявка стоит первой — спред верха точен
            money = best["money"]
            levels, partial = 1, False
        elif want:
            v = vwap_for(ladder, want, face, accrued)
            if not vwap_passes(v, want):
                continue
            price = round(v["px"], 4)
            depth_px = v["last_px"]
            val = _price_y_idx(isin, row, v["px"], side, exact)
            if val is None and not exact and v["levels"] == 1:
                # набор уложился в один уровень — VWAP-цена и есть верх стакана,
                # его спред точен (а не приближение), наклон тут не нужен
                val = top_val
            money = v["money"]
            levels = v["levels"]
            partial = v["partial"]
        else:
            price = depth_px = row.get(side)
            # верх стакана: у метрик он уже посчитан точно (y_idx_by_price по
            # bid/ask), но при exact сверяем тем же путём — на случай, если
            # котировка ушла вперёд снимка метрик
            val = _price_y_idx(isin, row, price, side, exact) if exact else top_val
            money = side_money_rub(depth_map.get(isin), side, face, accrued)
            levels, partial = None, False

        # Накопленный объём — по ГРАНИЦЕ набора (цена последнего взятого
        # уровня), а не по средневзвесу: тот всегда лучше худшего уровня, и по
        # нему накопление выходило меньше самого набора. Для верха стакана и
        # одной заявки граница совпадает с ценой сигнала.
        level_rub = money_upto(ladder, depth_px, side, face, accrued)

        # спред нужен для отсева ТОЛЬКО когда заданы границы: фильтр «крупные
        # заявки в ААА» не должен терять бумагу из-за непосчитанного Y-IDX
        if lo is not None or hi is not None:
            if val is None:
                continue
            if lo is not None and val < lo:
                continue
            if hi is not None and val > hi:
                continue
        # объём «по нашим условиям» — метрика повторного сигнала, не отбора:
        # на попадание бумаги в набор она не влияет (это делают spread/money выше)
        money_ok = money_in_spread(ladder, row, side, lo, hi, face, accrued, isin)
        # ЛУЧШАЯ ЛИ ЗАЯВКА: цена сигнала совпала с верхом стакана своей стороны.
        # Заменяет счёт уровней в уведомлении: «3 ур» описывало механику набора,
        # а стол спрашивает другое — стоит ли эта заявка первой в очереди.
        top_px = row.get(side)
        best_px = (price is not None and top_px is not None
                   and abs(float(price) - float(top_px)) < 1e-9)
        out.append({"isin": isin, "name": u.get("name") or isin,
                    "best": best_px,
                    "money_ok_rub": money_ok,
                    # спред может быть неизвестен, если его границы не заданы
                    # (фильтр «крупные заявки») и наклон Y-IDX не посчитался
                    "val_bps": round(val, 1) if val is not None else None,
                    "price": price, "money_rub": money,
                    # деньги ПО ЦЕНЕ СИГНАЛА (весь уровень/уровни набора) —
                    # именно это показывают человеку: money_rub в режиме порога
                    # равен самому порогу, а сумма стороны — вообще про другое
                    "level_money_rub": level_rub,
                    "levels": levels, "partial": partial, "single_px": single_px,
                    "rating": u.get("rating"), "emitter": u.get("emitter_name"),
                    # формула купона в уведомление: «КС + 1,2% (12)» отвечает,
                    # ЧЕМ бумага платит — без этого спред висит в воздухе
                    "base": u.get("base_rate_type"),
                    "margin_bps": u.get("spread_issue_bps"),
                    "cpy": u.get("coupons_per_year"),
                    "years": u.get("_years")})
    # по убыванию спреда; бумаги без спреда — в конец, по убыванию денег
    out.sort(key=lambda m: (m["val_bps"] is not None, m["val_bps"] or 0,
                            m["money_rub"] or 0), reverse=True)
    return out


def evaluate(params: dict, uni: List[dict], metrics: dict, depth_map: dict,
             today: Optional[date] = None) -> List[dict]:
    """Полный прогон (статика + рынок) — для разовых вызовов: превью формы,
    телеграм-скринер. Постоянный мониторинг держит статику отдельно."""
    return evaluate_candidates(params, static_candidates(params, uni, today, metrics),
                               metrics, depth_map, today=today)


async def market_snapshot():
    """(uni, metrics, depth_map) — общий снимок рынка для прогона фильтров.
    Пустой metrics = движок ещё не прогрелся, звать evaluate бессмысленно."""
    from services import depth as depth_svc, instruments_registry
    from services.market_data import market_cache
    metrics = market_cache.get("universe_metrics") or {}
    if not metrics:
        return [], {}, {}
    uni = await instruments_registry.fetch_floater_universe()
    return uni, metrics, depth_svc.get_depth()
