"""Живой средневзвес (VWAP) и дневной оборот по тикам Alor.

Свой VWAP, а не биржевой WAPRICE: считается по тем же тикам Alor, что лежат в
архиве и рисуются слоем «Средневзвес» на графике — цифра в таблице и линия на
графике обязаны сходиться. Тем же счётом берётся и оборот дня (Σ value, ₽): у
биржевого VALTODAY из ISS-снапшота видна задержка, а тик приходит сразу.

Охват. Раньше агрегат жил только по подписанным бумагам (избранное, потолок
ALOR_LIVE_CAP), теперь — по всему флоатер-юниверсу: тики по нему и так летят в
services/trades_stream и пишутся в архив, так что дневной счёт стоит двух
сложений на тик. Подъём с открытия сессии — seed_universe() одним запросом по
всем бумагам сразу (per-isin ensure_day остаётся для подписки: он ещё и
дотягивает хвост из Alor REST).

Состояние живёт в памяти процесса: при рестарте поднимается заново из архива.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))
# Хвост id, по которому отсекаются дубли на стыке «архив ↔ поток»: Alor при
# подписке отдаёт последние сделки (existing=true), часть из них уже в архиве.
# Больше держать незачем — стык это единицы сотен сделок, дальше поток уникален.
# Потолок держит и память: агрегаты теперь по всему юниверсу (~1300 бумаг), без
# него активная бумага унесла бы весь свой дневной поток id в set.
_SEEN_CAP = 2000


class _DayVwap:
    """Накопитель Σ(price·qty) / Σ(qty) и Σ value за один торговый день по бумаге."""
    __slots__ = ("day", "num", "den", "val", "n", "seen", "ready", "last_ts")

    def __init__(self, day: str):
        self.day = day
        self.num = 0.0      # Σ price·qty, цена в % номинала
        self.den = 0.0      # Σ qty, штук
        self.val = 0.0      # Σ value, ₽ без НКД (оборот дня)
        self.n = 0          # число сделок в агрегате
        self.seen: set = set()
        self.ready = False  # архив уже поднят
        self.last_ts: Optional[str] = None

    @property
    def vwap(self) -> Optional[float]:
        return round(self.num / self.den, 4) if self.den else None

    def add(self, price: float, qty: float, tid=None, ts: Optional[str] = None,
            value: Optional[float] = None) -> bool:
        """Добавляет сделку. False — дубль (уже учтена).

        value — рублёвый объём тика; без него оборот по бумаге не копится (цена
        в % номинала, а номинал знает только вызывающий слой)."""
        if not qty or price is None:
            return False
        if tid is not None:
            if tid in self.seen:
                return False
            self.seen.add(tid)
            if len(self.seen) > _SEEN_CAP:
                # держим только хвост: дубли возможны лишь на стыке с архивом
                self.seen = set(list(self.seen)[-_SEEN_CAP // 2:])
        self.num += float(price) * float(qty)
        self.den += float(qty)
        if value:
            self.val += float(value)
        self.n += 1
        if ts and (self.last_ts is None or ts > self.last_ts):
            self.last_ts = ts
        return True


_state: dict[str, _DayVwap] = {}


def _today() -> str:
    """Торговый день по МСК — тот же базис, что у архива тиков."""
    return datetime.now(_MSK).date().isoformat()


def _slot(isin: str) -> _DayVwap:
    """Накопитель бумаги на сегодня; на смене дня начинается с нуля."""
    day = _today()
    st = _state.get(isin)
    if st is None or st.day != day:
        st = _state[isin] = _DayVwap(day)
    return st


def read_day_ticks(isin: str, day: str) -> list[tuple]:
    """[(trade_id, price, qty, ts, value)] сделок дня из архива. Синхронный
    SQLite — зовётся только через to_thread."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute(
            "SELECT trade_id, price, qty, ts, value FROM trade_tick "
            "WHERE isin=? AND ts >= ? ORDER BY ts", (isin, day)).fetchall()
    return [(r["trade_id"], r["price"], r["qty"], r["ts"], r["value"]) for r in rows]


def read_day_ticks_all(day: str) -> list[tuple]:
    """То же по ВСЕМ бумагам за день, одним проходом: подъём юниверса по одной
    бумаге — это 1300 запросов на старте вместо одного."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute(
            "SELECT isin, trade_id, price, qty, ts, value FROM trade_tick "
            "WHERE ts >= ? ORDER BY ts", (day,)).fetchall()
    return [(r["isin"], r["trade_id"], r["price"], r["qty"], r["ts"], r["value"])
            for r in rows]


async def seed_universe(isins) -> int:
    """Поднимает дневные агрегаты пачки бумаг из архива (идемпотентно).

    Зовётся при сборке шардов тикового стрима: к моменту, когда фронт спросит
    оборот, счёт уже идёт с открытия сессии, а не с подписки. В Alor не ходим —
    хвост архива доливает hourly_bars_worker, а свежее приезжает потоком."""
    want = {i for i in isins if i}
    day = _today()
    todo = [i for i in want if not (_state.get(i) and _state[i].day == day
                                    and _state[i].ready)]
    if not todo:
        return 0
    try:
        rows = await asyncio.to_thread(read_day_ticks_all, day)
    except Exception as e:
        logger.warning("live vwap seed: %s", e)
        return 0
    todo_set = set(todo)
    for i in todo:
        st = _slot(i)
        st.ready = True      # даже если сделок нет: пустой день — валидное состояние
    n = 0
    for isin, tid, price, qty, ts, value in rows:
        if isin not in todo_set:
            continue
        cur = _state.get(isin)
        if cur is None or cur.day != day:
            continue
        if cur.add(price, qty, tid=tid, ts=ts, value=value):
            n += 1
    logger.info("live vwap: поднято %d сделок по %d бумагам за %s", n, len(todo), day)
    return n


async def ensure_day(isin: str, drain: bool = True) -> None:
    """Поднимает дневной агрегат из архива (идемпотентно, один раз на день).

    drain=False — не ходить в Alor (тест/оффлайн): агрегат соберётся по тому,
    что уже есть в архиве."""
    st = _slot(isin)
    if st.ready:
        return
    st.ready = True          # ставим ДО await: параллельные подписки не должны дублировать
    day = st.day
    if drain:
        try:
            from services import trades_archive as ta
            await ta.drain(isin, days=1)
        except Exception as e:
            logger.debug("live vwap drain %s: %s", isin, e)
    try:
        rows = await asyncio.to_thread(read_day_ticks, isin, day)
    except Exception as e:
        logger.warning("live vwap archive %s: %s", isin, e)
        return
    cur = _state.get(isin)
    if cur is None or cur.day != day:
        return               # день сменился, пока читали — накопитель уже новый
    for tid, price, qty, ts, value in rows:
        cur.add(price, qty, tid=tid, ts=ts, value=value)


def add_trade(isin: str, price: float, qty: float, tid=None, ts: Optional[str] = None,
              value: Optional[float] = None) -> None:
    """Сделка из потока Alor → в дневной агрегат."""
    _slot(isin).add(price, qty, tid=tid, ts=ts, value=value)


def get(isin: str) -> Optional[dict]:
    """{vwap_pct, volume, val_today, trades} или None, если сегодня сделок нет.

    volume — штуки, val_today — рубли без НКД (аналог биржевого VALTODAY)."""
    st = _state.get(isin)
    if st is None or st.day != _today() or not st.den:
        return None
    return {"vwap_pct": st.vwap, "volume": st.den, "trades": st.n,
            "val_today": round(st.val) if st.val else None}


def drop(isin: str) -> None:
    """Снимает состояние отписавшейся бумаги — карта не должна расти за аптайм."""
    _state.pop(isin, None)


def active() -> list:
    return list(_state)
