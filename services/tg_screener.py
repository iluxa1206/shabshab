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

# рейтинги, встречающиеся в реестре (без модификаторов +/-)
RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"]

# Отбор бумаг: три селектора, объединяемые по ИЛИ (бумага подходит, если попала
# хоть в один); пустые селекторы = весь рынок. Поверх — условия сделки (сторона,
# диапазон спреда, деньги в стакане), они всегда И.
_PARAM_DEFAULTS = {
    "ratings": [],          # ['AAA','AA'] — ИЛИ
    "emitters": [],         # ['Газпром капитал'] — ИЛИ, точное имя из реестра
    "isins": [],            # ['RU000A10AU99'] — ИЛИ
    "side": "ask",          # 'ask' — оффер (можно купить) | 'bid' — бид (продать)
    "spread_min": None,     # Y-IDX бп, нижняя граница диапазона
    "spread_max": None,     # Y-IDX бп, верхняя граница
    "min_money_rub": None,  # деньги на выбранной стороне стакана, руб
}

_MAX_SELECTOR_ITEMS = 50


class FilterError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if len(out) > _MAX_SELECTOR_ITEMS:
        raise FilterError(f"{field}: не больше {_MAX_SELECTOR_ITEMS} значений")
    return out


def normalize_params(raw: dict) -> dict:
    raw = raw or {}
    p = dict(_PARAM_DEFAULTS)
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

def _selected(u: dict, params: dict) -> bool:
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


def _side_money_rub(ladder: Optional[dict], side: str, face: float) -> Optional[float]:
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


def evaluate(params: dict, uni: List[dict], metrics: dict, depth_map: dict,
             today: Optional[date] = None) -> List[dict]:
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
        if not _selected(u, params):
            continue
        val = row.get("yoi_ask") if side == "ask" else row.get("yoi_bid")
        if val is None:
            continue
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        face = row.get("face_px") or 1000.0
        money = _side_money_rub(depth_map.get(isin), side, face)
        if params["min_money_rub"] is not None and (money or 0) < params["min_money_rub"]:
            continue
        out.append({"isin": isin, "name": u.get("name") or isin,
                    "val_bps": val, "price": row.get(side), "money_rub": money})
    out.sort(key=lambda m: m["val_bps"], reverse=True)
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


def format_message(fname: str, matches: List[dict], side: str = "ask") -> str:
    label = "оффер" if side == "ask" else "бид"
    lines = [f"<b>{fname}</b> — новые бумаги ({label}):"]
    for m in matches[:MAX_PER_MESSAGE]:
        px = f" @ {m['price']:.2f}" if m.get("price") is not None else ""
        money = (f", {m['money_rub'] / 1e6:.1f} млн ₽ в стакане"
                 if m.get("money_rub") else "")
        lines.append(f"• {m['name']} (<code>{m['isin']}</code>) — "
                     f"{m['val_bps']:.0f} бп{px}{money}")
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
            await telegram.send_message(
                user["chat_id"],
                format_message(f["name"], fresh, f["params"]["side"]))
            sent += 1
        except Exception as e:
            logger.warning("screener filter %s error: %s", f.get("id"), e)
    return sent
