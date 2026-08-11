"""Скринер-фильтры Telegram-бота: «любая бумага рынка, где Y-IDX проходит
порог при ограничениях (база/рейтинг/срок/объём в стакане)» → сообщение в чат.

Хранилище — portfolio.db (tg_filters + tg_filter_hits). Цикл гоняет
api.main.tg_screener_worker в торговые часы: метрики из
market_cache['universe_metrics'] (движок universe_stream), глубина из
services.depth — скринер сам ничего не считает и не ходит в сеть.

Анти-спам: (filter_id, isin) уходит в чат не чаще cooldown_min фильтра;
повтор — той же строкой tg_filter_hits (fired_at обновляется)."""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

MAX_PER_MESSAGE = 8         # бумаг в одном сообщении, остальное — «+N ещё»

# шкала национальных рейтингов (ACRA/ЭкспРА); выше индекс — хуже кредит
_RATING_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
                 "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-"]
_RATING_RANK = {r: i for i, r in enumerate(_RATING_ORDER)}

_PARAM_DEFAULTS = {
    "op": ">=",             # '>=' | '<='
    "threshold": 250.0,     # бп
    "src": "ask",           # 'ask' — Y-IDX по аску (реально купить) | 'last'
    "base": None,           # 'KEYRATE' | 'RUONIA' | None (любая)
    "rating_min": None,     # 'AA-' → пускаем AAA..AA-; None — без фильтра
    "max_years": None,      # лет до погашения максимум
    "min_depth_rub": None,  # руб на ask-стороне стакана минимум
}


class FilterError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_params(raw: dict) -> dict:
    p = dict(_PARAM_DEFAULTS)
    for k in p:
        if raw.get(k) is not None:
            p[k] = raw[k]
    if p["op"] not in (">=", "<="):
        raise FilterError("op: >= | <=")
    if p["src"] not in ("ask", "last"):
        raise FilterError("src: ask | last")
    if p["base"] not in (None, "KEYRATE", "RUONIA"):
        raise FilterError("base: KEYRATE | RUONIA | пусто")
    if p["rating_min"] is not None and p["rating_min"] not in _RATING_RANK:
        raise FilterError(f"rating_min: {' '.join(_RATING_ORDER)}")
    try:
        p["threshold"] = float(p["threshold"])
        for k in ("max_years", "min_depth_rub"):
            if p[k] is not None:
                p[k] = float(p[k])
                if p[k] <= 0:
                    raise ValueError
    except (TypeError, ValueError):
        raise FilterError("threshold/max_years/min_depth_rub — положительные числа")
    return p


# --- CRUD (per tg_user_id) ---

def create(tg_user_id: int, name: str, params: dict, cooldown_min: int = 240) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise FilterError("Имя фильтра: 1–60 символов")
    cooldown_min = int(cooldown_min or 240)
    if not (10 <= cooldown_min <= 2880):
        raise FilterError("cooldown_min: 10–2880")
    p = normalize_params(params)
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT INTO tg_filters(tg_user_id,name,params_json,cooldown_min,created_at) "
            "VALUES(?,?,?,?,?)",
            (tg_user_id, name, json.dumps(p, ensure_ascii=False), cooldown_min, _now()))
        fid = cur.lastrowid
    return get(fid)


def get(fid: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM tg_filters WHERE id=?", (fid,)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    d["enabled"] = bool(d["enabled"])
    return d


def list_for_user(tg_user_id: int) -> List[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM tg_filters WHERE tg_user_id=? ORDER BY id",
                         (tg_user_id,)).fetchall()
    return [_row(r) for r in rows]


def list_enabled() -> List[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM tg_filters WHERE enabled=1").fetchall()
    return [_row(r) for r in rows]


def update(tg_user_id: int, fid: int, *, name: Optional[str] = None,
           enabled: Optional[bool] = None, params: Optional[dict] = None,
           cooldown_min: Optional[int] = None) -> Optional[dict]:
    f = get(fid)
    if not f or f["tg_user_id"] != tg_user_id:
        return None
    sets, args = [], []
    if name is not None:
        name = name.strip()
        if not name or len(name) > 60:
            raise FilterError("Имя фильтра: 1–60 символов")
        sets.append("name=?"); args.append(name)
    if enabled is not None:
        sets.append("enabled=?"); args.append(1 if enabled else 0)
    if params is not None:
        sets.append("params_json=?")
        args.append(json.dumps(normalize_params(params), ensure_ascii=False))
    if cooldown_min is not None:
        cooldown_min = int(cooldown_min)
        if not (10 <= cooldown_min <= 2880):
            raise FilterError("cooldown_min: 10–2880")
        sets.append("cooldown_min=?"); args.append(cooldown_min)
    if sets:
        with _lock, _connect() as c:
            c.execute(f"UPDATE tg_filters SET {', '.join(sets)} WHERE id=?",
                      (*args, fid))
    return get(fid)


def delete(tg_user_id: int, fid: int) -> bool:
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM tg_filters WHERE id=? AND tg_user_id=?",
                        (fid, tg_user_id))
        if cur.rowcount:
            c.execute("DELETE FROM tg_filter_hits WHERE filter_id=?", (fid,))
        return cur.rowcount > 0


# --- движок ---

def _rating_ok(rating: Optional[str], rating_min: Optional[str]) -> bool:
    if rating_min is None:
        return True
    rank = _RATING_RANK.get((rating or "").strip().upper())
    if rank is None:
        return False        # нет/незнаком рейтинг — консервативно мимо
    return rank <= _RATING_RANK[rating_min]


def _years_left(maturity_iso: Optional[str], today: date) -> Optional[float]:
    try:
        return (date.fromisoformat(maturity_iso) - today).days / 365.25
    except (TypeError, ValueError):
        return None


def _ask_depth_rub(ladder: Optional[dict], face: float) -> Optional[float]:
    """Σ руб по ask-стороне снимка глубины {'a': [[px_pct, qty], ...]}."""
    if not ladder:
        return None
    total = 0.0
    for lvl in (ladder.get("a") or []):
        try:
            px, qty = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        total += px / 100.0 * face * qty
    return total or None


def evaluate(params: dict, uni: List[dict], metrics: dict, depth_map: dict,
             today: Optional[date] = None) -> List[dict]:
    """Матчи фильтра по рынку → [{isin, name, val_bps, price, depth_rub}],
    отсортированы по «интересности» (val по направлению op)."""
    today = today or date.today()
    out = []
    for u in uni:
        isin = u.get("isin")
        row = metrics.get(isin)
        if not row:
            continue
        if row.get("implausible") or row.get("price_stale") or row.get("price_thin"):
            continue
        if params["base"] and u.get("base_rate_type") != params["base"]:
            continue
        if not _rating_ok(u.get("rating"), params["rating_min"]):
            continue
        if params["max_years"] is not None:
            yl = _years_left(u.get("maturity_date"), today)
            if yl is None or yl > params["max_years"]:
                continue
        val = row.get("yoi_ask") if params["src"] == "ask" else row.get("yoi")
        if val is None:
            continue
        if params["op"] == ">=" and val < params["threshold"]:
            continue
        if params["op"] == "<=" and val > params["threshold"]:
            continue
        face = row.get("face_px") or 1000.0
        depth_rub = _ask_depth_rub(depth_map.get(isin), face)
        if params["min_depth_rub"] is not None and (depth_rub or 0) < params["min_depth_rub"]:
            continue
        price = row.get("ask") if params["src"] == "ask" else row.get("last")
        out.append({"isin": isin, "name": u.get("name") or isin,
                    "val_bps": val, "price": price, "depth_rub": depth_rub})
    out.sort(key=lambda m: m["val_bps"], reverse=(params["op"] == ">="))
    return out


def fresh_matches(fid: int, cooldown_min: int, matches: List[dict]) -> List[dict]:
    """Отсекает бумаги, уже уходившие в чат внутри cooldown; свежие помечает
    (fired_at upsert) и возвращает."""
    if not matches:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_min)).isoformat()
    now = _now()
    fresh = []
    with _lock, _connect() as c:
        for m in matches:
            r = c.execute("SELECT fired_at FROM tg_filter_hits WHERE filter_id=? AND isin=?",
                          (fid, m["isin"])).fetchone()
            if r and r["fired_at"] > cutoff:
                continue
            c.execute("INSERT INTO tg_filter_hits(filter_id,isin,fired_at) VALUES(?,?,?) "
                      "ON CONFLICT(filter_id,isin) DO UPDATE SET fired_at=excluded.fired_at",
                      (fid, m["isin"], now))
            fresh.append(m)
    return fresh


def format_message(fname: str, matches: List[dict]) -> str:
    lines = [f"🔎 <b>{fname}</b> — новые бумаги:"]
    for m in matches[:MAX_PER_MESSAGE]:
        px = f" @ {m['price']:.2f}" if m.get("price") is not None else ""
        dr = (f", стакан {m['depth_rub'] / 1e6:.1f} млн ₽"
              if m.get("depth_rub") else "")
        lines.append(f"• {m['name']} (<code>{m['isin']}</code>) — "
                     f"{m['val_bps']:.0f} бп{px}{dr}")
    if len(matches) > MAX_PER_MESSAGE:
        lines.append(f"…и ещё {len(matches) - MAX_PER_MESSAGE}")
    return "\n".join(lines)


async def run_cycle() -> int:
    """Один проход: все enabled-фильтры против текущего снапшота рынка.
    Возвращает число отправленных сообщений."""
    from services import depth as depth_svc, instruments_registry, telegram, tg_users
    from services.market_data import market_cache

    filters = list_enabled()
    if not filters or not telegram.enabled():
        return 0
    metrics = market_cache.get("universe_metrics") or {}
    if not metrics:
        return 0            # движок метрик ещё не прогрелся
    uni = await instruments_registry.fetch_floater_universe()
    depth_map = depth_svc.get_depth()

    sent = 0
    for f in filters:
        try:
            user = tg_users.get(f["tg_user_id"])
            if not user or user.get("muted"):
                continue
            matches = evaluate(f["params"], uni, metrics, depth_map)
            fresh = fresh_matches(f["id"], f["cooldown_min"], matches)
            if not fresh:
                continue
            await telegram.send_message(user["chat_id"],
                                        format_message(f["name"], fresh))
            sent += 1
        except Exception as e:
            logger.warning("screener filter %s error: %s", f.get("id"), e)
    return sent
