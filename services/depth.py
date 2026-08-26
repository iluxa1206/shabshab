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
    поллер ещё не отработал или снимок протух.

    СВЕЖЕСТЬ ПО ШАРДАМ, а не по рынку целиком. Стрим держит по сокету на 150
    бумаг, и глобальный depth_ts обновлял ЛЮБОЙ живой шард: смерть одного
    сокета пряталась за соседями, а скринер сигналил «5 млн на офере» по
    заявке, снятой утром. stream_watchdog это не ловит — он смотрит на
    непустоту кэша.

    Протухшие ISIN ИЗЫМАЮТСЯ из словаря, а не отдаются пустой лестницей:
    {"b": [], "a": []} прочиталось бы потребителем как «в стакане ничего нет»
    и дало бы честный на вид ноль объёма вместо «данных нет»."""
    d = market_cache.get("depth") or {}
    ts = market_cache.get("depth_ts") or 0.0
    if not d or time.time() - ts > _STALE_SEC:
        return {}
    shard_ts = market_cache.get("depth_shard_ts") or {}
    shard_isins = market_cache.get("depth_shard_isins") or {}
    if not shard_isins:
        # СТРИМ ВООБЩЕ НЕ ПОДНИМАЛСЯ — работает батч-поллер, метка одна на всех.
        # Проверять тут пустоту shard_ts НЕЛЬЗЯ: когда стрим был и умер ЦЕЛИКОМ
        # (реконнект-шторм, протухший токен), метки исчезают, а списки бумаг
        # остаются — и мы бы отдали весь протухший стакан как свежий, то есть
        # исходный баг в максимальном масштабе.
        return d
    now = time.time()
    dead = {i for sid, isins in shard_isins.items()
            for i in isins if now - (shard_ts.get(sid) or 0.0) > _STALE_SEC}
    if dead:
        # ЖИВОЙ ШАРД ПОБЕЖДАЕТ ОТСТАВНОЙ СПИСОК. depth_shard_isins не снимается
        # ни при смерти сокета, ни при пересборке пула, а shard_id
        # переиспользуются — без этого вычитания бумага, которую прямо сейчас
        # ведёт живой шард, выбрасывалась по списку шарда, которого уже нет.
        dead -= {i for sid, isins in shard_isins.items()
                 if now - (shard_ts.get(sid) or 0.0) <= _STALE_SEC for i in isins}
    if dead:
        # БАТЧ-ПОЛЛЕР ПЕРЕБИВАЕТ МЁРТВЫЙ ШАРД. depth_stream_covers включает
        # HTTP-фолбэк уже при потере 20% шардов, и он честно обновляет ВЕСЬ
        # юниверс — выбрасывать эти бумаги по устаревшей шардовой метке значит
        # терять свежие данные ровно тогда, когда стрим деградировал.
        batch = market_cache.get("depth_batch") or {}
        if now - (batch.get("ts") or 0.0) <= _STALE_SEC:
            dead -= (batch.get("isins") or set())
    return {k: v for k, v in d.items() if k not in dead} if dead else d


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
    now = time.time()
    market_cache["depth_ts"] = now
    # отметка батча: get_depth() не должен выбрасывать бумаги, которые поллер
    # только что обновил, из-за метки шарда, чей сокет умер
    market_cache["depth_batch"] = {"ts": now, "isins": set(fresh)}
    return len(fresh)
