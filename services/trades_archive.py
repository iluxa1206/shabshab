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


# Ограничитель обращений к Alor. Массовый прогон (1300 бумаг × пагинация) на
# concurrency 4 упирался в rate-limit: 429 на данных И таймауты/SSL-обрывы на
# самом oauth.alor.ru — токен-эндпоинт живёт под тем же лимитом, что данные.
_ALOR_SEM = asyncio.Semaphore(2)
_TOKEN_LOCK = asyncio.Lock()
_token_cache: dict = {"headers": None, "at": 0.0}
_TOKEN_TTL = 10 * 60          # свой TTL короче серверного (28 мин у auth.py)
_RETRY_STATUS = (429, 500, 502, 503, 504)


async def _headers(force: bool = False) -> Optional[dict]:
    """Заголовок с bearer'ом. Токен кэшируется в процессе: без этого каждая
    бумага массового прогона дёргала oauth и он начинал рвать соединения."""
    import time as _time
    now = _time.monotonic()
    if not force and _token_cache["headers"] and now - _token_cache["at"] < _TOKEN_TTL:
        return _token_cache["headers"]
    async with _TOKEN_LOCK:
        now = _time.monotonic()
        if not force and _token_cache["headers"] and now - _token_cache["at"] < _TOKEN_TTL:
            return _token_cache["headers"]
        from auth import get_access_token, REFRESH_TOKEN
        if not REFRESH_TOKEN:
            return None
        token = None
        for attempt in range(3):
            try:
                token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
            except Exception as e:      # таймаут/SSL к oauth — ждём и пробуем ещё
                logger.warning("alor token attempt %d: %s", attempt + 1, e)
                token = None
            if token:
                break
            await asyncio.sleep(2 ** attempt)
        if not token:
            return None
        _token_cache["headers"] = {"Authorization": f"Bearer {token}"}
        _token_cache["at"] = _time.monotonic()
        return _token_cache["headers"]


async def _alor_get(client: httpx.AsyncClient, url: str, headers: dict, params: dict):
    """GET к Alor под семафором, с бэкоффом на 429/5xx и одним ре-логином на 401."""
    async with _ALOR_SEM:
        for attempt in range(4):
            try:
                r = await client.get(url, headers=headers, params=params, timeout=60.0)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == 3:
                    logger.warning("alor %s: %s", url.rsplit("/", 2)[-2], e)
                    return None
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt == 0:
                fresh = await _headers(force=True)
                if fresh:
                    headers = fresh
                continue
            if r.status_code in _RETRY_STATUS and attempt < 3:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return r
    return None


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
        r = await _alor_get(client, url, headers, {**params, "offset": offset})
        if r is None:
            break
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
    r = await _alor_get(client, f"{BASE_API}/md/v2/Securities/MOEX/{isin}/alltrades",
                        headers, {"format": "Simple", "take": _PAGE})
    if r is None:
        return []
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
    today = datetime.now(_MSK).date()      # день считаем по МСК, как биржа
    frm = datetime.combine(today - timedelta(days=days), datetime.min.time(), _MSK)
    # Ровно полночь Alor уже относит к «сегодня» и отвечает 400 ("From and To
    # must be less than today") — отступаем на секунду во вчера. Сегодняшние
    # сделки всё равно приходят отдельным вызовом /alltrades.
    to = datetime.combine(today, datetime.min.time(), _MSK) - timedelta(seconds=1)

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
