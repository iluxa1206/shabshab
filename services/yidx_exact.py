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


# Когда по бумаге последний раз жаловались на отказ расчёта: одна битая бумага
# пересчитывается каждые несколько секунд, и без троттлинга лог станет
# бесполезным.
_WARN_TTL_SEC = 300.0
_warned: Dict[str, float] = {}


def _warn_once(isin, err) -> None:
    import time
    now = time.time()
    if now - _warned.get(str(isin), 0.0) < _WARN_TTL_SEC:
        return
    _warned[str(isin)] = now
    logger.warning("точный Y-IDX не посчитан для %s: %s", isin, err)


def y_idx_many(ctx: dict, prices: Iterable[float]) -> Dict[float, Optional[int]]:
    """{цена: Y-IDX бп} по методике на тёплом контексте (bond_details.load_reprice_ctx).

    ctx без биржевого НКД считается ПО СВОЕМУ начислению (services/accrued:
    купон опубликован → спека фиксинга → прошлый купон → индекс+маржа). Раньше
    здесь стояло молчание — «выдумывать нельзя», — и бумага без биржевого НКД
    получала прочерк на весь день. Но лестница ошибается на копейки (сверка
    25.08: 32,93 против факта 32,97), а прочерк не говорит ничего вовсе.
    Расчёт помечает такое число (`accrued_estimated`), молчим только когда НКД
    не даёт ни биржа, ни лестница (pricing_status NO_ACCRUED → пустой ответ).
    """
    want: List[float] = []
    for p in prices or []:
        if p is None:
            continue
        k = _key(p)
        if k > 0 and k not in want:
            want.append(k)
    if not want or not ctx:
        return {}
    from services.valuation import calculate_valuation_metrics, horizon_at_price
    try:
        m = calculate_valuation_metrics(
            ctx["ref_obj"], want[0], ctx["curve"], ctx["calc_date"],
            accrued_override=ctx.get("accrued_live"), periods=ctx.get("periods"),
            amorts=ctx.get("amorts"), offers=ctx.get("offers"),
            ruonia_curve=ctx.get("ruonia_curve"),
            accrued_date=ctx.get("accrued_date"),
            # МАРЖИ ЗДЕСЬ НИКТО НЕ ЧИТАЕТ: наружу идёт только Y-IDX по ценам, а
            # SM/DM — это два солвера, 78–92 % расчёта бумаги, причём DM ещё и
            # ПЕРЕСОБИРАЕТ поток на плоской кривой. Замер: 12,8 → 9,2 мс на 61
            # платеже, 105 → 62 мс на 361.
            with_margins=False,
            # КЭШ ПОТОКОВ ОБЩИЙ С ВИТРИНОЙ: ключи, кривая и день те же, чистится
            # он на смене дня/кривых/правке Справочника. Без него дешёвая ветка
            # сторон пересобирала поток 2–4 раза за проход мимо кэша, который
            # для этой же бумаги уже заполнен (те же 61 платёж: 9,2 → 7,1 мс,
            # 361 платёж: 62 → 28 мс).
            flows_cache=ctx.get("flows_cache"),
            alt_prices=want)
    except Exception as e:
        # НЕ debug: в проде этот уровень выключен, и отказ расчёта выглядел как
        # молчаливый прочерк в сторонах — причину искать было негде. Троттлим по
        # бумаге, чтобы одна битая не залила лог.
        _warn_once(ctx.get("isin"), e)
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
