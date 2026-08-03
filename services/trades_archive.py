"""Тиковый архив сделок (Alor) и обогащение часовых баров сторонами сделок.

Зачем свой архив: у брокера обезличенные сделки живут ~30 календарных дней
(замерено бисекцией на RU000A109K40: −25д отдаёт, −35д пусто), у MOEX ISS
trades.json — только текущая сессия. Глубже 30 дней тиков нет НИГДЕ бесплатно,
поэтому копим сами: демон сливает окно с перехлёстом, дальше история наша.

Что даёт тик, чего нет в свече:
  • side (агрессор buy/sell) → эффективный спред = VWAP_buy − VWAP_sell;
  • отдельные крупные сделки (фильтр по value) — в свече они размазаны.

Alor: GET /md/v2/Securities/MOEX/{ISIN}/alltrades[/history]
  • /history: from,to (unix sec, ОБА строго < сегодня, иначе 400), limit ≤ 50000,
    offset, поле total — по нему пагинируем;
  • без /history: текущая сессия, параметр take.
Поля ответа (format=Simple): id (== TRADENO у MOEX), qty (бумаг), price (% номинала),
time (UTC ISO), side. VALUE Alor не отдаёт — считаем qty*face*price/100 (без НКД,
сверено с MOEX trades.json: qty=1, price=100.02 → VALUE=1000.2 при номинале 1000).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

ALOR_HISTORY_DAYS = 30       # реальная глубина обезличенных сделок у брокера
_PAGE = 50000                # максимум limit у Alor
_MSK = timezone(timedelta(hours=3))
_DEFAULT_FACE = 1000.0


async def _headers() -> Optional[dict]:
    from auth import get_access_token, REFRESH_TOKEN
    if not REFRESH_TOKEN:
        return None
    token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
    return {"Authorization": f"Bearer {token}"} if token else None


def _msk_ts(iso_utc: str) -> str:
    """'2026-07-24T04:13:07.129Z' → '2026-07-24 07:13:07' (МСК, как у свечей ISS)."""
    s = iso_utc.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:                      # микросекунды длиннее 6 знаков
        head, _, tail = s.partition(".")
        dt = datetime.fromisoformat(f"{head}.{tail[:6]}{tail[-6:]}" if "+" in tail
                                    else f"{head}+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────── загрузка из Alor ───────────────────────────

async def fetch_history(client: httpx.AsyncClient, isin: str, headers: dict,
                        frm: datetime, to: datetime) -> list[dict]:
    """Сделки в [frm, to). Обе границы строго раньше сегодняшнего дня."""
    from auth import BASE_API
    url = f"{BASE_API}/md/v2/Securities/MOEX/{isin}/alltrades/history"
    params = {"from": int(frm.timestamp()), "to": int(to.timestamp()),
              "format": "Simple", "limit": _PAGE}
    out: list[dict] = []
    offset = 0
    while True:
        r = await client.get(url, headers=headers, params={**params, "offset": offset},
                             timeout=60.0)
        if r.status_code != 200:
            logger.warning("alor trades %s %s: %s", isin, r.status_code, r.text[:160])
            break
        j = r.json()
        chunk = j.get("list") or []
        out.extend(chunk)
        total = j.get("total") or 0
        offset += len(chunk)
        if not chunk or offset >= total:
            break
    return out


async def fetch_today(client: httpx.AsyncClient, isin: str, headers: dict) -> list[dict]:
    """Сделки текущей сессии (у /history сегодняшний день недоступен)."""
    from auth import BASE_API
    r = await client.get(f"{BASE_API}/md/v2/Securities/MOEX/{isin}/alltrades",
                         headers=headers, params={"format": "Simple", "take": _PAGE},
                         timeout=60.0)
    if r.status_code != 200:
        logger.warning("alor today %s %s: %s", isin, r.status_code, r.text[:160])
        return []
    j = r.json()
    return j if isinstance(j, list) else (j.get("list") or [])


# ─────────────────────────── запись ───────────────────────────

def upsert_ticks(isin: str, raw: list[dict], faces: dict[str, float],
                 fallback_face: float = _DEFAULT_FACE) -> int:
    """INSERT OR IGNORE по (isin, trade_id): перехлёст окон не плодит дублей."""
    rows = []
    for t in raw:
        tid, price, qty = t.get("id"), t.get("price"), t.get("qty")
        if tid is None or price is None or not qty:
            continue
        ts = _msk_ts(str(t.get("time") or ""))
        day = ts[:10]
        face = faces.get(day) or fallback_face
        prev = [d for d in faces if d <= day]
        if day not in faces and prev:
            face = faces[max(prev)]
        rows.append((isin, int(tid), ts, float(price), float(qty),
                     round(float(qty) * face * float(price) / 100, 2),
                     t.get("side"), t.get("board")))
    if not rows:
        return 0
    with _lock, _connect() as c:
        cur = c.executemany(
            "INSERT OR IGNORE INTO trade_tick(isin,trade_id,ts,price,qty,value,side,board) "
            "VALUES(?,?,?,?,?,?,?,?)", rows)
        return cur.rowcount or 0


async def drain(isin: str, days: int = ALOR_HISTORY_DAYS,
                board: Optional[str] = None, include_today: bool = True) -> int:
    """Скачивает сделки бумаги за последние `days` дней (обрезается глубиной
    брокера) + текущую сессию, пишет в архив. Возвращает число НОВЫХ строк."""
    headers = await _headers()
    if not headers:
        logger.warning("drain %s: нет кредов Alor", isin)
        return 0
    days = min(days, ALOR_HISTORY_DAYS)
    today = date.today()
    frm = datetime.combine(today - timedelta(days=days), datetime.min.time(), _MSK)
    to = datetime.combine(today, datetime.min.time(), _MSK)   # строго < сегодня

    # номиналы по дням — для рублёвого объёма амортизируемых бумаг
    from services.bars import fetch_daily_face
    from services.backdate import resolve_market
    secid, brd = await resolve_market(isin, board)
    async with httpx.AsyncClient() as mc:
        faces = await fetch_daily_face(mc, secid or isin, brd or "TQCB",
                                       frm.date().isoformat(), today.isoformat())

    async with httpx.AsyncClient() as client:
        raw = await fetch_history(client, isin, headers, frm, to)
        if include_today:
            raw += await fetch_today(client, isin, headers)
    fallback = max(faces.values()) if faces else _DEFAULT_FACE
    return upsert_ticks(isin, raw, faces, fallback)


# ─────────────────────────── чтение / агрегация ───────────────────────────

def read_trades(isin: str, frm: Optional[str] = None, till: Optional[str] = None,
                min_value: float = 0, side: Optional[str] = None,
                limit: int = 2000) -> list[dict]:
    """Сделки по возрастанию времени. min_value — порог в рублях (крупные)."""
    q = "SELECT * FROM trade_tick WHERE isin=?"
    args: list = [isin]
    if frm:
        q += " AND ts >= ?"
        args.append(frm)
    if till:
        q += " AND ts <= ?"
        args.append(till + " 23:59:59" if len(till) == 10 else till)
    if min_value:
        q += " AND value >= ?"
        args.append(min_value)
    if side in ("buy", "sell"):
        q += " AND side = ?"
        args.append(side)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _connect() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in reversed(rows)]


def enrich_bars_with_ticks(isin: str, frm: Optional[str] = None,
                           till: Optional[str] = None) -> int:
    """Досчитывает по тикам часовые trades/buy_volume/sell_volume/buy_vwap/sell_vwap
    и проставляет их в bar_hourly. Строку бара не создаёт (её делают свечи) —
    UPDATE только там, где бар уже есть."""
    q = ("SELECT substr(ts,1,13)||':00' h, side, SUM(qty) q, SUM(value) v, COUNT(*) n "
         "FROM trade_tick WHERE isin=?")
    args: list = [isin]
    if frm:
        q += " AND ts >= ?"
        args.append(frm)
    if till:
        q += " AND ts <= ?"
        args.append(till + " 23:59:59" if len(till) == 10 else till)
    q += " GROUP BY h, side"
    with _connect() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    if not rows:
        return 0

    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["h"], {"trades": 0, "buy_q": 0.0, "buy_v": 0.0,
                                    "sell_q": 0.0, "sell_v": 0.0})
        a["trades"] += r["n"]
        if r["side"] == "buy":
            a["buy_q"] += r["q"] or 0
            a["buy_v"] += r["v"] or 0
        elif r["side"] == "sell":
            a["sell_q"] += r["q"] or 0
            a["sell_v"] += r["v"] or 0

    upd = []
    for h, a in agg.items():
        # vwap стороны в % номинала: value/qty уже рубли за бумагу → делим на номинал
        # бара (face берём из строки bar_hourly в SQL-выражении ниже)
        upd.append((a["trades"], a["buy_q"] or None, a["sell_q"] or None,
                    a["buy_v"] or None, a["sell_v"] or None, isin, h))
    with _lock, _connect() as c:
        cur = c.executemany(
            "UPDATE bar_hourly SET trades=?1, buy_volume=?2, sell_volume=?3, "
            "buy_vwap = CASE WHEN ?2 IS NOT NULL AND COALESCE(face,1000)>0 "
            "                THEN ROUND(?4/?2/COALESCE(face,1000)*100, 4) END, "
            "sell_vwap = CASE WHEN ?3 IS NOT NULL AND COALESCE(face,1000)>0 "
            "                 THEN ROUND(?5/?3/COALESCE(face,1000)*100, 4) END "
            "WHERE isin=?6 AND ts=?7", upd)
        return cur.rowcount or 0


def stats(isin: str) -> dict:
    """Что уже накоплено по бумаге — для диагностики/статуса."""
    with _connect() as c:
        r = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM trade_tick WHERE isin=?",
                      (isin,)).fetchone()
        b = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM bar_hourly WHERE isin=?",
                      (isin,)).fetchone()
    return {"ticks": r["n"], "ticks_from": r["a"], "ticks_till": r["b"],
            "bars": b["n"], "bars_from": b["a"], "bars_till": b["b"]}
