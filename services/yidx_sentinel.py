"""Сторож согласия двух путей Y-IDX.

Спред одной бумаги на проде считается ДВУМЯ независимыми путями:
  • движок метрик (`universe_stream._crunch` → `universe.enrich_bond`) — число
    в таблице, оно же якорь наклона для лестницы стакана;
  • точный пересчёт (`screener_core.exact_y_idx` на тёплом контексте) — число
    события скринера и уровней стакана.
В отдельном процессе оба дают одно и то же. На проде 27.08.2026 в телеграм
ушли два сообщения по ВЭБ2Р-53 с одной ценой и одной книгой, но спредами 188
и 166 бп — то есть в живом воркере пути расходятся, а воспроизвести это
снаружи нельзя: состояние движка живёт только в его памяти.

Сторож раз в минуту берёт горсть бумаг и сравнивает пути на ОДНОЙ цене. Пишет
в лог только расхождения — из строки видно, какой путь уехал и с какими
ингредиентами (индекс-база, возраст контекста, живость кривой).
"""
import asyncio
import logging
import os
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

INTERVAL_SEC = float(os.getenv("YIDX_SENTINEL_SEC", "60"))
BATCH = int(os.getenv("YIDX_SENTINEL_BATCH", "25"))
TOL_BPS = float(os.getenv("YIDX_SENTINEL_TOL_BPS", "10"))

_cursor = 0                      # обходим универс по кругу, а не одни и те же
_seen: Dict[str, float] = {}     # isin → когда последний раз ругались


def _sample(metrics: dict) -> List[str]:
    """Следующая горсть бумаг с ценой — по кругу, чтобы за час обойти весь рынок."""
    global _cursor
    ids = [i for i, r in metrics.items()
           if r and r.get("last") is not None and r.get("yoi") is not None]
    if not ids:
        return []
    ids.sort()
    start = _cursor % len(ids)
    _cursor = start + BATCH
    return (ids + ids)[start:start + BATCH]


def _check(isins: List[str], metrics: dict) -> tuple:
    """Синхронная часть (в heavy-пуле): reprice на тёплых контекстах.
    → (расхождения, сколько пар удалось сравнить, максимум |Δ| бп)."""
    from services.screener_core import exact_y_idx, y_idx_diag
    out, compared, worst = [], 0, 0.0
    for isin in isins:
        row = metrics.get(isin) or {}
        px = row.get("last")
        if px is None:
            continue
        ex = exact_y_idx(isin, px)
        row_y = row.get("yoi")
        if ex is None or row_y is None:
            continue
        compared += 1
        d = abs(float(ex) - float(row_y))
        worst = max(worst, d)
        if d < TOL_BPS:
            continue
        out.append({"isin": isin, "px": px, "row_yoi": row_y, "exact": ex,
                    "diag": y_idx_diag(isin, px, row, "ask")})
    return out, compared, worst


async def run_forever() -> None:
    from services.heavy import run_heavy
    from services.market_data import market_cache
    from services.screener_core import warm_exact_ctx

    await asyncio.sleep(90)      # даём движку метрик прогреться
    while True:
        try:
            await asyncio.sleep(INTERVAL_SEC)
            metrics = market_cache.get("universe_metrics") or {}
            if not metrics:
                continue
            ids = _sample(metrics)
            if not ids:
                continue
            await warm_exact_ctx(ids)
            bad, compared, worst = await run_heavy(_check, ids, metrics)
            now = time.time()
            # ПУЛЬС: без него «расхождений нет» и «сторож не крутится» выглядят
            # в логе одинаково — пустотой
            logger.info("Y-IDX сторож: сверено %d из %d, максимум |Δ| %.0f бп, "
                        "расхождений %d", compared, len(ids), worst, len(bad))
            for b in bad or []:
                # одна бумага — не чаще раза в 10 минут, иначе лог утонет
                if now - _seen.get(b["isin"], 0) < 600:
                    continue
                _seen[b["isin"]] = now
                logger.warning(
                    "Y-IDX РАСХОЖДЕНИЕ %s @ %s: таблица %s vs точный %s (Δ%+.0f) | %s",
                    b["isin"], b["px"], b["row_yoi"], b["exact"],
                    float(b["exact"]) - float(b["row_yoi"]), b["diag"])
            if bad:
                logger.warning("Y-IDX сторож: расхождений %d из %d в выборке",
                               len(bad), len(ids))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("yidx sentinel: %s", e)
