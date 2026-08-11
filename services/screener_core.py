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
from typing import List, Optional

logger = logging.getLogger(__name__)

# рейтинги, встречающиеся в реестре (без модификаторов +/-)
RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"]

PARAM_DEFAULTS = {
    "ratings": [],          # ['AAA','AA'] — ИЛИ
    "emitters": [],         # ['Газпром капитал'] — ИЛИ, точное имя из реестра
    "isins": [],            # ['RU000A10AU99'] — ИЛИ
    "side": "ask",          # 'ask' — оффер (можно купить) | 'bid' — бид (продать)
    "spread_min": None,     # Y-IDX бп, нижняя граница диапазона
    "spread_max": None,     # Y-IDX бп, верхняя граница
    "min_money_rub": None,  # деньги на выбранной стороне стакана, руб
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
    for k in ("spread_min", "spread_max", "min_money_rub"):
        v = raw.get(k)
        if v is None or v == "":
            continue
        try:
            p[k] = float(v)
        except (TypeError, ValueError):
            raise FilterError(f"{k}: должно быть числом")
    if p["min_money_rub"] is not None and p["min_money_rub"] <= 0:
        raise FilterError("min_money_rub: положительное число")
    if (p["spread_min"] is not None and p["spread_max"] is not None
            and p["spread_min"] > p["spread_max"]):
        raise FilterError("Диапазон спреда: «от» больше «до»")
    if p["spread_min"] is None and p["spread_max"] is None:
        raise FilterError("Задай хотя бы одну границу спреда")
    return p


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


def side_money_rub(ladder: Optional[dict], side: str, face: float) -> Optional[float]:
    """Σ руб по выбранной стороне снимка глубины {'a'|'b': [[px_pct, qty], ...]}."""
    if not ladder:
        return None
    total = 0.0
    for lvl in (ladder.get("a" if side == "ask" else "b") or []):
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        total += px / 100.0 * face * qty
    return total or None


def evaluate(params: dict, uni: List[dict], metrics: dict, depth_map: dict) -> List[dict]:
    """Матчи фильтра по рынку → [{isin, name, val_bps, price, money_rub}],
    по убыванию спреда (сначала самые широкие)."""
    side = params["side"]
    lo, hi = params["spread_min"], params["spread_max"]
    out = []
    for u in uni:
        isin = u.get("isin")
        row = metrics.get(isin)
        if not row:
            continue
        if row.get("implausible") or row.get("price_stale") or row.get("price_thin"):
            continue
        if not selected(u, params):
            continue
        val = row.get("yoi_ask") if side == "ask" else row.get("yoi_bid")
        if val is None:
            continue
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        face = row.get("face_px") or 1000.0
        money = side_money_rub(depth_map.get(isin), side, face)
        if params["min_money_rub"] is not None and (money or 0) < params["min_money_rub"]:
            continue
        out.append({"isin": isin, "name": u.get("name") or isin,
                    "val_bps": val, "price": row.get(side), "money_rub": money,
                    "rating": u.get("rating"), "emitter": u.get("emitter_name")})
    out.sort(key=lambda m: m["val_bps"], reverse=True)
    return out


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
