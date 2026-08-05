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
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from services.portfolio_db import DB_PATH, _connect, _lock

logger = logging.getLogger(__name__)

ALOR_HISTORY_DAYS = 30       # реальная глубина обезличенных сделок у брокера

# ── ретеншен архива ──────────────────────────────────────────────────────────
# Без него trade_tick рос без границы: ~6.7 млн строк за месяц (≈1.9 ГБ вместе с
# индексами), а data/ — bind-mount тома на VPS.
#
# Что тик даёт после того, как час уже закрыт:
#   1) buy/sell VWAP часа — считается ОДИН раз (enrich_bars_with_ticks) и живёт
#      дальше в самой строке bar_hourly, тик для этого больше не нужен;
#   2) лента крупных принтов (/history/{isin}/trades) — фронт запрашивает её с
#      порогом от 1 млн ₽ (BIG_THRESHOLDS в ChartPage), мельче не показывает.
# Поэтому за пределами сырого окна держим только крупные сделки: это 0.54% строк
# и полная сохранность всего, что UI умеет отрисовать.
#
# Сырое окно можно держать коротким: дрейн инкрементальный (идёт от водяного
# знака tick_drain), поэтому удалённую мелочь он заново НЕ качает. Нижняя граница
# — лишь бы окно перекрывало нахлёст дрейна, иначе прун и дрейн начнут качели.
# Дефолт 35 дней (> глубины брокера) оставлен консервативным: сузить до 7–10 —
# это ~450 МБ вместо ~2 ГБ, но старую мелочь уже не вернуть.
TICK_DRAIN_OVERLAP_HOURS = float(os.getenv("TICK_DRAIN_OVERLAP_HOURS", "6"))
# минимум сырого окна — нахлёст дрейна плюс сутки запаса
MIN_RAW_DAYS = int(TICK_DRAIN_OVERLAP_HOURS // 24) + 2
TICK_RAW_DAYS = max(int(os.getenv("TICK_RAW_DAYS", "35")), MIN_RAW_DAYS)
TICK_BIG_VALUE_RUB = float(os.getenv("TICK_BIG_VALUE_RUB", "1000000"))
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
                        frm: datetime, to: datetime) -> tuple[list[dict], bool]:
    """Сделки в [frm, to). Обе границы строго раньше сегодняшнего дня.

    Возвращает (строки, полностью_ли_вычитано). Флаг нужен водяному знаку:
    пагинация обрывается на первой сетевой ошибке и отдаёт ЧАСТЬ окна — сдвинуть
    по такому ответу границу дрейна значит навсегда потерять недокачанный хвост.
    """
    from auth import BASE_API
    url = f"{BASE_API}/md/v2/Securities/MOEX/{isin}/alltrades/history"
    params = {"from": int(frm.timestamp()), "to": int(to.timestamp()),
              "format": "Simple", "limit": _PAGE}
    out: list[dict] = []
    offset = 0
    while True:
        r = await _alor_get(client, url, headers, {**params, "offset": offset})
        if r is None:
            return out, False
        if r.status_code != 200:
            logger.warning("alor trades %s %s: %s", isin, r.status_code, r.text[:160])
            return out, False
        j = r.json()
        chunk = j.get("list") or []
        out.extend(chunk)
        total = j.get("total") or 0
        offset += len(chunk)
        if not chunk or offset >= total:
            return out, True


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


def get_watermark(isin: str) -> Optional[str]:
    """До какого момента история сделок бумаги вычитана целиком (или None)."""
    with _connect() as c:
        r = c.execute("SELECT last_ts FROM tick_drain WHERE isin=?", (isin,)).fetchone()
    return r["last_ts"] if r else None


def set_watermark(isin: str, last_ts: str) -> None:
    """Двигает знак только ВПЕРЁД: параллельные дрейны одной бумаги (демон и
    открытая карточка) не должны откатывать друг друга назад."""
    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _connect() as c:
        c.execute(
            "INSERT INTO tick_drain(isin,last_ts,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(isin) DO UPDATE SET last_ts=MAX(last_ts,excluded.last_ts), "
            "updated_at=excluded.updated_at",
            (isin, last_ts, now))


def seed_watermarks() -> int:
    """Разовая миграция: знак для бумаг, накопленных ДО инкрементального дрейна.

    Тот архив собирался полными 30-дневными прогонами, поэтому «вычитано до
    последней известной сделки» — оценка заведомо консервативная: хвост от неё
    перекачается ещё раз, но дыры не возникнет. Без сида каждая бумага один раз
    сходила бы за месяцем сделок — ровно то, от чего уходим. Идемпотентно."""
    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO tick_drain(isin,last_ts,updated_at) "
            "SELECT isin, MAX(ts), ? FROM trade_tick GROUP BY isin", (now,))
        return cur.rowcount or 0


async def drain(isin: str, days: int = ALOR_HISTORY_DAYS, board: Optional[str] = None,
                include_today: bool = True, full: bool = False) -> int:
    """Доливает сделки бумаги в архив. Возвращает число НОВЫХ строк.

    ИНКРЕМЕНТАЛЬНО: качаем от водяного знака (минус нахлёст), а не всегда всё
    30-дневное окно брокера. Раньше каждое открытие графика перекачивало месяц
    сделок целиком — на ликвидной бумаге это 700k сделок, 15 страниц по 50k, на
    КАЖДЫЙ запрос; часовой демон делал то же самое по всему юниверсу.

    `days` — потолок окна (глубже брокер всё равно ничего не отдаёт), он же
    рабочее окно на холодную, когда знака ещё нет. `full=True` игнорирует знак
    и перекачивает окно целиком (бэкфилл/починка дыры).

    Сегодняшняя сессия тянется отдельным вызовом всегда: /history её не отдаёт,
    и водяной знак в сегодня НЕ заводим — правой границей всегда конец вчера.
    Значит завтрашний дрейн перечитает сегодня уже через /history и закроет то,
    что могло не поместиться в take-лимит текущей сессии."""
    headers = await _headers()
    if not headers:
        logger.warning("drain %s: нет кредов Alor", isin)
        return 0
    days = min(days, ALOR_HISTORY_DAYS)
    today = datetime.now(_MSK).date()      # день считаем по МСК, как биржа
    floor = datetime.combine(today - timedelta(days=days), datetime.min.time(), _MSK)
    # Ровно полночь Alor уже относит к «сегодня» и отвечает 400 ("From and To
    # must be less than today") — отступаем на секунду во вчера.
    to = datetime.combine(today, datetime.min.time(), _MSK) - timedelta(seconds=1)

    frm = floor
    if not full:
        wm = get_watermark(isin)
        if wm:
            try:
                mark = datetime.fromisoformat(wm).replace(tzinfo=_MSK)
                # нахлёст на случай сделок, доехавших с опозданием/не по порядку;
                # дубли отсекает INSERT OR IGNORE по (isin, trade_id)
                frm = max(floor, mark - timedelta(hours=TICK_DRAIN_OVERLAP_HOURS))
            except ValueError:
                logger.warning("drain %s: битый водяной знак %r", isin, wm)
    if frm > to:                      # знак уже за концом вчера — только сегодня
        frm = to

    # номиналы по дням — для рублёвого объёма амортизируемых бумаг
    from services.bars import fetch_daily_face
    from services.backdate import resolve_market
    secid, brd = await resolve_market(isin, board)
    async with httpx.AsyncClient() as mc:
        faces = await fetch_daily_face(mc, secid or isin, brd or "TQCB",
                                       frm.date().isoformat(), today.isoformat())

    async with httpx.AsyncClient() as client:
        raw, complete = await fetch_history(client, isin, headers, frm, to)
        if include_today:
            raw += await fetch_today(client, isin, headers)
    fallback = max(faces.values()) if faces else _DEFAULT_FACE
    # вставка тысяч тиков — синхронный SQLite: на ликвидной бумаге executemany
    # держал event loop десятки мс, а демон зовёт drain по всему юниверсу
    n = await asyncio.to_thread(upsert_ticks, isin, raw, faces, fallback)
    # знак двигаем ТОЛЬКО на полностью вычитанном окне — и даже если сделок в нём
    # не было: «тишина» тоже вычитана, иначе неликвид качал бы месяц вечно
    if complete:
        set_watermark(isin, to.strftime("%Y-%m-%d %H:%M:%S"))
    return n


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


def _tape_where(frm: Optional[str], till: Optional[str], min_value: float,
                side: Optional[str], isins: Optional[list[str]]) -> tuple[str, list]:
    """Общий WHERE ленты рынка — чтобы строки и агрегат считались по одному фильтру."""
    q, args = " WHERE 1=1", []
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
    if isins:
        q += f" AND isin IN ({','.join('?' * len(isins))})"
        args.extend(isins)
    return q, args


def read_tape(frm: Optional[str] = None, till: Optional[str] = None,
              min_value: float = 0, side: Optional[str] = None,
              isins: Optional[list[str]] = None, limit: int = 500) -> list[dict]:
    """Лента сделок по ВСЕМУ рынку (не по одной бумаге), новые сверху.

    Источник тот же архив, что и у карточки выпуска: его наливает часовой демон
    по всему юниверсу, поэтому лента отстаёт от биржи максимум на один прогон.
    За сырым окном ретеншена (TICK_RAW_DAYS) в архиве остаются только принты от
    TICK_BIG_VALUE_RUB — глубокая лента по определению «крупная»."""
    where, args = _tape_where(frm, till, min_value, side, isins)
    q = ("SELECT isin, trade_id, ts, price, qty, value, side, board FROM trade_tick"
         + where + " ORDER BY ts DESC, trade_id DESC LIMIT ?")
    args.append(limit)
    with _connect() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def tape_stats(frm: Optional[str] = None, till: Optional[str] = None,
               min_value: float = 0, side: Optional[str] = None,
               isins: Optional[list[str]] = None, top: int = 10) -> dict:
    """Итоги окна ленты — по ВСЕМ подходящим сделкам, а не по срезанным лимитом:
    обороты buy/sell и топ бумаг по рублёвому обороту."""
    where, args = _tape_where(frm, till, min_value, side, isins)
    with _connect() as c:
        rows = c.execute("SELECT side, COUNT(*) n, SUM(value) v, SUM(qty) q "
                         "FROM trade_tick" + where + " GROUP BY side", args).fetchall()
        tops = c.execute("SELECT isin, COUNT(*) n, SUM(value) v FROM trade_tick" + where +
                         " GROUP BY isin ORDER BY v DESC LIMIT ?", [*args, top]).fetchall()
        last = c.execute("SELECT MAX(ts) t FROM trade_tick").fetchone()
    by = {r["side"]: {"n": r["n"], "value": r["v"] or 0, "qty": r["q"] or 0} for r in rows}
    return {"n": sum(r["n"] for r in rows),
            "value": sum((r["v"] or 0) for r in rows),
            "buy_value": by.get("buy", {}).get("value", 0),
            "sell_value": by.get("sell", {}).get("value", 0),
            "issuers_top": [{"isin": r["isin"], "n": r["n"], "value": r["v"] or 0} for r in tops],
            "archive_till": last["t"] if last else None}


def enrich_bars_with_ticks(isin: str, frm: Optional[str] = None,
                           till: Optional[str] = None) -> int:
    """Досчитывает по тикам часовые trades/buy_volume/sell_volume/buy_vwap/sell_vwap
    и проставляет их в bar_hourly. Строку бара не создаёт (её делают свечи) —
    UPDATE только там, где бар уже есть.

    Окно ЖЁСТКО обрезается сырым окном ретеншена: за ним от часа остались только
    крупные принты, и пересчёт по ним затёр бы верный агрегат частичным. Старые
    бары уже обогащены и трогать их не нужно."""
    floor = raw_floor()
    frm = max(frm, floor) if frm else floor
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


def raw_floor(raw_days: Optional[int] = None) -> str:
    """Нижняя граница сырого окна (ISO-дата, МСК): раньше неё в архиве только
    крупные принты."""
    d = TICK_RAW_DAYS if raw_days is None else max(int(raw_days), MIN_RAW_DAYS)
    return (datetime.now(_MSK).date() - timedelta(days=d)).isoformat()


def prune(raw_days: Optional[int] = None, big_value: Optional[float] = None,
          dry_run: bool = False) -> dict:
    """Ретеншен trade_tick: за сырым окном оставляем только сделки от big_value ₽.

    Идёт по одному ISIN — попадает в ix_tick_isin_ts(isin, ts) и держит каждую
    транзакцию короткой (одним DELETE по всей таблице это фулскан 6.7 млн строк
    под локом WAL-писателя).

    dry_run=True считает то же самое, ничего не удаляя."""
    floor = raw_floor(raw_days)
    thr = TICK_BIG_VALUE_RUB if big_value is None else float(big_value)
    where = "isin=? AND ts < ? AND (value IS NULL OR value < ?)"
    with _connect() as c:
        isins = [r[0] for r in c.execute("SELECT DISTINCT isin FROM trade_tick")]
    deleted, touched = 0, 0
    for isin in isins:
        if dry_run:
            with _connect() as c:
                n = c.execute(f"SELECT COUNT(*) FROM trade_tick WHERE {where}",
                              (isin, floor, thr)).fetchone()[0]
        else:
            with _lock, _connect() as c:
                n = c.execute(f"DELETE FROM trade_tick WHERE {where}",
                              (isin, floor, thr)).rowcount or 0
        if n:
            deleted += n
            touched += 1
    return {"deleted": deleted, "isins": touched, "scanned_isins": len(isins),
            "floor": floor, "big_value": thr, "dry_run": dry_run}


def vacuum() -> dict:
    """VACUUM базы: SQLite не отдаёт освободившиеся страницы ОС сам (auto_vacuum=0).

    Дорого и блокирующе: переписывает файл целиком и требует свободного места
    примерно в размер БД. Зовётся только после заметного пруна и вне торговых
    часов. На тесном диске VPS отказываемся заранее: VACUUM, упавший на «no space
    left», оставит после себя ещё и полудописанный временный файл."""
    import shutil
    import time as _time
    before = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    free = shutil.disk_usage(DB_PATH.parent).free
    if free < before * 1.2:
        return {"skipped": "мало места на диске", "before_mb": round(before / 1e6, 1),
                "free_mb": round(free / 1e6, 1)}
    t0 = _time.monotonic()
    with _lock:
        c = _connect()
        try:
            c.execute("VACUUM")
        finally:
            c.close()
    after = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"before_mb": round(before / 1e6, 1), "after_mb": round(after / 1e6, 1),
            "freed_mb": round((before - after) / 1e6, 1),
            "secs": round(_time.monotonic() - t0, 1)}


def db_stats() -> dict:
    """Объём архива — для /api/status и логов обслуживания."""
    with _connect() as c:
        t = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM trade_tick").fetchone()
        b = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM bar_hourly").fetchone()
        w = c.execute("SELECT COUNT(*) n, MIN(last_ts) a FROM tick_drain").fetchone()
    return {"ticks": t["n"], "ticks_from": t["a"], "ticks_till": t["b"],
            "bars": b["n"], "bars_from": b["a"], "bars_till": b["b"],
            "db_mb": round(DB_PATH.stat().st_size / 1e6, 1) if DB_PATH.exists() else None,
            "raw_days": TICK_RAW_DAYS, "big_value_rub": TICK_BIG_VALUE_RUB,
            # отстающий знак = бумага, которую дрейн давно не закрывал целиком
            "watermarks": w["n"], "watermark_oldest": w["a"],
            "drain_overlap_h": TICK_DRAIN_OVERLAP_HOURS}


def stats(isin: str) -> dict:
    """Что уже накоплено по бумаге — для диагностики/статуса."""
    with _connect() as c:
        r = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM trade_tick WHERE isin=?",
                      (isin,)).fetchone()
        b = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM bar_hourly WHERE isin=?",
                      (isin,)).fetchone()
    return {"ticks": r["n"], "ticks_from": r["a"], "ticks_till": r["b"],
            "bars": b["n"], "bars_from": b["a"], "bars_till": b["b"]}
