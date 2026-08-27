"""Y-IDX ПО МЕТОДИКЕ для набора цен — один расчёт на бумагу, много цен.

Зачем: линейная оценка от якоря (`screener_core.y_idx_at`) честна только рядом
с якорем и только пока якорь свеж. 27.08.2026 на проде уехал сам якорь (строка
метрик считалась без биржевого НКД), и наклон послушно увёл за собой ВСЮ
лестницу стакана в телеграме — расхождение было не в одном уровне, а во всех
сразу. Линия через кривую точку не спасает: её надо не подпирать, а не
проводить.

Почему это по карману (замер на проде 27.08.2026):
  • отдельный `reprice_at_price` на цену           — 85 мс;
  • ОДИН `calculate_valuation_metrics` + alt_prices — 13 мс на 9 цен ≈ 1,5 мс
    на цену.
Поток, кривая и base leg от цены не зависят и строятся один раз — отсюда
разница в 58 раз. Поэтому цены лестницы считаем ПАЧКОЙ, а не поштучно.

Горизонт выбирается для КАЖДОЙ цены тем же правилом цены
(`services.valuation.horizon_at_price`): у бумаги с офертой соседние уровни
законно могут прайситься к разным горизонтам.
"""
import logging
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_PX_DIGITS = 4


def _key(px: float) -> float:
    return round(float(px), _PX_DIGITS)


def y_idx_many(ctx: dict, prices: Iterable[float]) -> Dict[float, Optional[int]]:
    """{цена: Y-IDX бп} по методике на тёплом контексте (bond_details.load_reprice_ctx).

    ctx без биржевого НКД (`accrued_missing`) считать нельзя: начисление
    придётся выдумывать, а десятые доли рубля стоят десятков б.п. — молчим.
    """
    want: List[float] = []
    for p in prices or []:
        if p is None:
            continue
        k = _key(p)
        if k > 0 and k not in want:
            want.append(k)
    if not want or not ctx or ctx.get("accrued_missing"):
        return {}
    from services.valuation import calculate_valuation_metrics, horizon_at_price
    try:
        m = calculate_valuation_metrics(
            ctx["ref_obj"], want[0], ctx["curve"], ctx["calc_date"],
            accrued_override=ctx.get("accrued_live"), periods=ctx.get("periods"),
            amorts=ctx.get("amorts"), offers=ctx.get("offers"),
            ruonia_curve=ctx.get("ruonia_curve"),
            accrued_date=ctx.get("accrued_date"),
            alt_prices=want)
    except Exception as e:
        logger.debug("y_idx_many %s: %s", ctx.get("isin"), e)
        return {}
    hzs = m.get("horizons") or {}
    out: Dict[float, Optional[int]] = {}
    for p in want:
        hz = horizon_at_price(p, m)
        sel = hzs.get(hz) or hzs.get("maturity") or {}
        v = (sel.get("y_idx_by_price") or {}).get(p)
        if v is None:            # горизонт без числа на этой цене — общий фолбэк
            v = (m.get("y_idx_by_price") or {}).get(p)
        out[p] = None if v is None else int(round(float(v)))
    return out
