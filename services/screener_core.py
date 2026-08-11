"""Ядро скринера: описание фильтра и его прогон по рынку. Общее для вкладки
СИГНАЛЫ (services/signals.py) и Telegram-бота (services/tg_screener.py) —
условия и цифры обязаны совпадать, поэтому логика живёт в одном месте, а
модули-владельцы отвечают только за хранение и доставку.

Фильтр = отбор бумаг + условия сделки:
  отбор — три селектора (рейтинги / эмитенты / ISIN), объединяются по ИЛИ;
          пусто во всех трёх = весь рынок;
  условия — сторона стакана, диапазон Y-IDX и деньги на этой стороне; всегда И.

Ничего не считает сам: метрики берутся из market_cache['universe_metrics']
(движок universe_stream), лестницы — из services.depth."""
import logging
import re
from datetime import date
from typing import List, Optional

logger = logging.getLogger(__name__)

# рейтинги, встречающиеся в реестре (без модификаторов +/-)
RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"]

# Субординация — по названию бумаги: отдельного признака в реестре нет (ни MOEX,
# ни corpbonds его не отдают), а маркер в short_name — устойчивая конвенция
# («ВТБСУБ1-12», «ВТБСУБТ1Р2»). Ловим и добавочный капитал (Т1/T1, перп).
_SUBORD_RE = re.compile(r"СУБ|SUB|ПЕРП|PERP|(?<![A-ZА-Я0-9])[TТ]1(?![0-9])", re.I)

PARAM_DEFAULTS = {
    "ratings": [],          # ['AAA','AA'] — ИЛИ
    "emitters": [],         # ['Газпром капитал'] — ИЛИ, точное имя из реестра
    "isins": [],            # ['RU000A10AU99'] — ИЛИ
    "side": "ask",          # 'ask' — оффер (можно купить) | 'bid' — бид (продать)
    "spread_min": None,     # Y-IDX бп, нижняя граница диапазона
    "spread_max": None,     # Y-IDX бп, верхняя граница
    "min_money_rub": None,  # деньги на выбранной стороне стакана, руб
    "money_mode": "book",   # 'book' — набрать сумму по лестнице (VWAP на тикет)
                            # 'single' — ОДНА заявка не меньше суммы (крупный принт)
    "years_min": None,      # лет до погашения, нижняя граница
    "years_max": None,      # лет до погашения, верхняя граница
    "hide_subord": False,   # прятать суборды (см. _SUBORD_RE)
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
    p["hide_subord"] = bool(raw.get("hide_subord"))
    for k in ("spread_min", "spread_max", "min_money_rub", "years_min", "years_max"):
        v = raw.get(k)
        if v is None or v == "":
            continue
        try:
            p[k] = float(v)
        except (TypeError, ValueError):
            raise FilterError(f"{k}: должно быть числом")
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
    for k in ("years_min", "years_max"):
        if p[k] is not None and p[k] < 0:
            raise FilterError("Срок до погашения: неотрицательное число")
    if (p["years_min"] is not None and p["years_max"] is not None
            and p["years_min"] > p["years_max"]):
        raise FilterError("Срок до погашения: «от» больше «до»")
    return p


def is_subord(u: dict) -> bool:
    return bool(_SUBORD_RE.search(u.get("name") or ""))


def years_left(maturity_iso: Optional[str], today: date) -> Optional[float]:
    try:
        return (date.fromisoformat(maturity_iso) - today).days / 365.25
    except (TypeError, ValueError):
        return None


def selected(u: dict, params: dict) -> bool:
    """Отбор бумаги селекторами: рейтинг ИЛИ эмитент ИЛИ ISIN. Ни одного
    селектора не задано → весь рынок."""
    sel_r, sel_e, sel_i = params["ratings"], params["emitters"], params["isins"]
    if not (sel_r or sel_e or sel_i):
        return True
    if sel_r and (u.get("rating") or "").strip().upper() in sel_r:
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


def level_money(px_pct: Optional[float], qty: Optional[float], face: float,
                accrued: float = 0.0) -> float:
    if px_pct is None or qty is None or not face:
        return 0.0
    return qty * (face * px_pct / 100.0 + (accrued or 0.0))


def vwap_for(levels, want_rub: float, face: float,
             accrued: float = 0.0) -> Optional[dict]:
    """Средневзвешенная цена набора want_rub рублей по лестнице (от лучшей цены).
    → {px, money, levels, partial} либо None. partial=True — глубины не хватило,
    px посчитан по всей книге."""
    if not levels or not (want_rub > 0) or not face:
        return None
    left, cost, taken, used = float(want_rub), 0.0, 0.0, 0
    for lvl in levels:
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        money = level_money(px, qty, face, accrued)
        if money <= 0:
            continue
        used += 1
        part = min(money, left)
        cost += part * px           # цену взвешиваем деньгами, не количеством
        taken += part
        left -= part
        if left <= 1e-9:
            break
    if taken <= 0:
        return None
    return {"px": cost / taken, "money": taken, "levels": used, "partial": left > 1e-9}


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


def y_idx_at(row: dict, px: Optional[float], side: str) -> Optional[float]:
    """Y-IDX по произвольной цене: линейно от известного якоря через наклон
    dY/dP. На масштабе стакана Y-IDX(цена) практически прямая (см. vwap.js)."""
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


def static_candidates(params: dict, uni: List[dict],
                      today: Optional[date] = None) -> List[dict]:
    """Отбор по НЕподвижным признакам: рейтинг/эмитент/ISIN, срок, суборд.
    Считается редко — множество меняется только с реестром, а не с рынком.
    Дальше мониторится только глубина этих бумаг (см. evaluate_candidates)."""
    today = today or date.today()
    ylo, yhi = params.get("years_min"), params.get("years_max")
    hide_sub = params.get("hide_subord")
    out = []
    for u in uni:
        if hide_sub and is_subord(u):
            continue
        yrs = years_left(u.get("maturity_date"), today)
        if ylo is not None or yhi is not None:
            # без даты погашения срок не проверить — такую бумагу не пускаем,
            # иначе фильтр «до 2 лет» молча притащит бессрочную
            if yrs is None:
                continue
            if ylo is not None and yrs < ylo:
                continue
            if yhi is not None and yrs > yhi:
                continue
        if not selected(u, params):
            continue
        out.append(dict(u, _years=round(yrs, 2) if yrs is not None else None))
    return out


def evaluate_candidates(params: dict, candidates: List[dict], metrics: dict,
                        depth_map: dict) -> List[dict]:
    """Рыночная часть: по уже отобранным бумагам считает цену/спред/деньги и
    отсеивает по диапазону спреда и объёму.

    Если задан min_money_rub — цена это СРЕДНЕВЗВЕС набора этого объёма по
    лестнице, а спред пересчитан к ней (иначе цифра относилась бы к верху
    стакана на 50 бумаг, а исполняться сделка будет по всей лестнице).
    Без объёма — верх стакана, как в таблице."""
    side = params["side"]
    lo, hi = params["spread_min"], params["spread_max"]
    want = params.get("min_money_rub")
    out = []
    for u in candidates:
        isin = u.get("isin")
        row = metrics.get(isin)
        if not row:
            continue
        if row.get("implausible") or row.get("price_stale") or row.get("price_thin"):
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
            if not best or best["money"] < want:
                continue
            price = single_px = best["price"]
            val = y_idx_at(row, price, side)
            if val is None and price == row.get(side):
                val = top_val          # заявка стоит первой — спред верха точен
            money = best["money"]
            levels, partial = 1, False
        elif want:
            v = vwap_for(ladder, want, face, accrued)
            if not vwap_passes(v, want):
                continue
            price = round(v["px"], 4)
            val = y_idx_at(row, v["px"], side)
            if val is None and v["levels"] == 1:
                # набор уложился в один уровень — VWAP-цена и есть верх стакана,
                # его спред точен (а не приближение), наклон тут не нужен
                val = top_val
            money = v["money"]
            levels = v["levels"]
            partial = v["partial"]
        else:
            price = row.get(side)
            val = top_val
            money = side_money_rub(depth_map.get(isin), side, face, accrued)
            levels, partial = None, False

        # спред нужен для отсева ТОЛЬКО когда заданы границы: фильтр «крупные
        # заявки в ААА» не должен терять бумагу из-за непосчитанного Y-IDX
        if lo is not None or hi is not None:
            if val is None:
                continue
            if lo is not None and val < lo:
                continue
            if hi is not None and val > hi:
                continue
        out.append({"isin": isin, "name": u.get("name") or isin,
                    # спред может быть неизвестен, если его границы не заданы
                    # (фильтр «крупные заявки») и наклон Y-IDX не посчитался
                    "val_bps": round(val, 1) if val is not None else None,
                    "price": price, "money_rub": money,
                    "levels": levels, "partial": partial, "single_px": single_px,
                    "rating": u.get("rating"), "emitter": u.get("emitter_name"),
                    "years": u.get("_years")})
    # по убыванию спреда; бумаги без спреда — в конец, по убыванию денег
    out.sort(key=lambda m: (m["val_bps"] is not None, m["val_bps"] or 0,
                            m["money_rub"] or 0), reverse=True)
    return out


def evaluate(params: dict, uni: List[dict], metrics: dict, depth_map: dict,
             today: Optional[date] = None) -> List[dict]:
    """Полный прогон (статика + рынок) — для разовых вызовов: превью формы,
    телеграм-скринер. Постоянный мониторинг держит статику отдельно."""
    return evaluate_candidates(params, static_candidates(params, uni, today),
                               metrics, depth_map)


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
