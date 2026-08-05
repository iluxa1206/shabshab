"""Живой средневзвес (VWAP) торгового дня по подписанным бумагам.

Свой VWAP, а не биржевой WAPRICE: считается по тем же тикам Alor, что лежат в
архиве и рисуются слоем «Средневзвес» на графике — цифра в таблице и линия на
графике обязаны сходиться.

Схема: при первой подписке дневной агрегат поднимается из trade_tick (архив
наливает часовой демон, перед подъёмом делаем инкрементальный drain, чтобы
закрыть хвост с последнего прогона), дальше состояние дополняется потоком
AllTradesGetAndSubscribe. Так VWAP полон с открытия сессии, а не с момента,
когда пользователь открыл вкладку.

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
_SEEN_CAP = 5000


class _DayVwap:
    """Накопитель Σ(price·qty) / Σ(qty) за один торговый день по одной бумаге."""
    __slots__ = ("day", "num", "den", "n", "seen", "ready", "last_ts")

    def __init__(self, day: str):
        self.day = day
        self.num = 0.0      # Σ price·qty, цена в % номинала
        self.den = 0.0      # Σ qty, штук
        self.n = 0          # число сделок в агрегате
        self.seen: set = set()
        self.ready = False  # архив уже поднят
        self.last_ts: Optional[str] = None

    @property
    def vwap(self) -> Optional[float]:
        return round(self.num / self.den, 4) if self.den else None

    def add(self, price: float, qty: float, tid=None, ts: Optional[str] = None) -> bool:
        """Добавляет сделку. False — дубль (уже учтена)."""
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
    """[(trade_id, price, qty, ts)] сделок дня из архива. Синхронный SQLite —
    зовётся только через to_thread."""
    from services.portfolio_db import _connect
    with _connect() as c:
        rows = c.execute(
            "SELECT trade_id, price, qty, ts FROM trade_tick "
            "WHERE isin=? AND ts >= ? ORDER BY ts", (isin, day)).fetchall()
    return [(r["trade_id"], r["price"], r["qty"], r["ts"]) for r in rows]


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
    for tid, price, qty, ts in rows:
        cur.add(price, qty, tid=tid, ts=ts)


def add_trade(isin: str, price: float, qty: float, tid=None, ts: Optional[str] = None) -> None:
    """Сделка из потока Alor → в дневной агрегат."""
    _slot(isin).add(price, qty, tid=tid, ts=ts)


def get(isin: str) -> Optional[dict]:
    """{vwap_pct, volume, trades} или None, если по бумаге сегодня нет сделок."""
    st = _state.get(isin)
    if st is None or st.day != _today() or not st.den:
        return None
    return {"vwap_pct": st.vwap, "volume": st.den, "trades": st.n}


def drop(isin: str) -> None:
    """Снимает состояние отписавшейся бумаги — карта не должна расти за аптайм."""
    _state.pop(isin, None)


def active() -> list:
    return list(_state)
