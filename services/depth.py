"""Кэш глубины стакана по всему юниверсу флоатеров.

Зачем: в таблице bid/ask — только ВЕРХ стакана (board-снапшот MOEX), а трейдеру
нужна цена, по которой реально исполнится тикет на N рублей (VWAP по лестнице) и
спред к этой цене. Верх стакана на 100 бумаг ничего не говорит о том, влезет ли
в него миллион.

Данные копит фоновый поллер (api.main.depth_poller) батч-снимком Alor WS —
per-isin HTTP по 540 бумагам в цикле не вариант. VWAP считает фронт: объём
задаёт пользователь, а лестница у него уже на руках."""
import logging
import time
from typing import Dict, List

from services.market_data import market_cache

logger = logging.getLogger(__name__)

_DEPTH_LEVELS = 20      # уровней на сторону в снимке
_STALE_SEC = 900        # снимок старше 15 мин наружу не отдаём (лучше пусто, чем врать)


def get_depth() -> Dict[str, dict]:
    """{isin: {"b": [[px, qty], ...], "a": [...]}} — последний снимок. Пусто, если
    поллер ещё не отработал или снимок протух."""
    d = market_cache.get("depth") or {}
    ts = market_cache.get("depth_ts") or 0.0
    if not d or time.time() - ts > _STALE_SEC:
        return {}
    return d


def depth_ts() -> float:
    return float(market_cache.get("depth_ts") or 0.0)


async def refresh_depth(isins: List[str], chunk: int = 150) -> int:
    """Снимает стаканы по isins чанками (один WS-заход на чанк) → market_cache.
    Бумаги, по которым Alor смолчал, сохраняют прошлый снимок — дырка в ответе не
    должна выключать фильтр по объёму на пол-рынка. Возвращает число обновлённых."""
    import asyncio
    from auth import alor_token
    from core.orderbooks import get_orderbooks_dict

    token = await alor_token()
    if not token:
        return 0
    fresh: Dict[str, dict] = {}
    for i in range(0, len(isins), chunk):
        part = isins[i:i + chunk]
        try:
            fresh.update(await get_orderbooks_dict(token, "MOEX", part, depth=_DEPTH_LEVELS))
        except Exception as e:
            logger.warning(f"depth chunk error: {e}")
        await asyncio.sleep(0.5)   # мягкий rate-limit между сокетами
    if not fresh:
        return 0
    cur = dict(market_cache.get("depth") or {})
    cur.update(fresh)
    market_cache["depth"] = cur
    market_cache["depth_ts"] = time.time()
    return len(fresh)
