"""Витрина анонсов первички (services/primary_calendar)."""
from fastapi import APIRouter, Depends, Path, Query

from api.routes.auth import require_admin

router = APIRouter()


@router.get("", tags=["Primary"])
@router.get("/", tags=["Primary"], include_in_schema=False)
async def get_primary_calendar():
    """Планируемые размещения: даты книги/размещения, ориентир купона, объём +
    спред по нашей модели (services/primary_pricing).

    Кэш выгрузки обновляется по TTL и раз в сутки из дневного синка; источник
    внешний, его падение отдаёт последний известный снимок. Спред считается
    ПОВЕРХ снимка на актуальных кривых — замораживать его в суточном кэше
    нельзя: кривая двигается в течение дня, а сравнение с монитором имеет смысл
    только когда обе цифры с одной кривой.

    Сбой прайсинга не роняет витрину: строки уедут без model, таблица покажет
    ориентир организатора текстом — как до появления колонки."""
    from services.primary_calendar import get_calendar
    from services.primary_pricing import price_rows_cached
    data = await get_calendar()
    try:
        models = await price_rows_cached(data["rows"])
        data["rows"] = [{**r, "model": m} for r, m in zip(data["rows"], models)]
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("первичка: спреды не посчитаны: %s", e)
    return data


@router.get("/placements", tags=["Primary"])
async def get_placements(
    date_from: str = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: str = Query(None, alias="to", description="YYYY-MM-DD"),
    q: str = Query(None, description="Поиск по имени/SECID"),
    min_rub: float = Query(0, ge=0, description="Порог объёма размещения, ₽"),
    limit: int = Query(500, ge=1, le=5000),
):
    """ФАКТ размещений с биржи: строка = выпуск (первый день, объём, цена).

    Здесь, в отличие от анонсов, ничего не прогнозируется — это дневные итоги
    бордов «Размещение» из ISS (services/primary_placements). Подписи (эмитент,
    тип купона, маржа, рейтинг) подмешиваются из реестра, если бумага в нём
    есть: у выпуска-«однодневки» и у неторгуемого более выпуска их не будет.
    """
    from services import primary_placements as pp
    from services import instruments_registry as reg
    from services.pools import run_bg

    rows = await run_bg(pp.aggregates, date_from, date_to, q, min_rub, limit)
    labels = await run_bg(reg.labels_map, [r["isin"] for r in rows if r.get("isin")])
    for r in rows:
        lab = labels.get(r.get("isin") or "") or {}
        # эмитент: наше каноническое имя, иначе полное юрлицо из справочника
        # MOEX — у 4/5 размещений года бумаги в реестре нет вовсе
        r["emitter"] = lab.get("emitter") or r.pop("emitent_moex", None)
        r["base"] = lab.get("base")                 # KEYRATE/RUONIA/FIXED/None
        r["rating"] = lab.get("rating")
        r["margin_bps"] = lab.get("margin_bps")
        r["maturity"] = lab.get("maturity")
        r["coupon_text"] = lab.get("coupon_text")
        r["coupons_per_year"] = lab.get("coupons_per_year")
        r["in_registry"] = bool(lab)
        r.pop("emitent_moex", None)
    return {"rows": rows, "stats": await run_bg(pp.stats)}


@router.get("/placements/{secid}/days", tags=["Primary"])
async def get_placement_days(secid: str = Path(..., min_length=4, max_length=24)):
    """Разворот строки: по каким дням и почём набирался объём (доразмещения)."""
    from services import primary_placements as pp
    from services.pools import run_bg
    return {"secid": secid, "rows": await run_bg(pp.days_of, secid)}


@router.post("/placements/sync", tags=["Primary"])
async def sync_placements(
    days: int = Query(30, ge=1, le=2000, description="Окно бэкфилла, кал. дней"),
    force: bool = Query(False, description="Перечитать уже собранные даты"),
    _admin: dict = Depends(require_admin),
):
    """Ручной бэкфилл истории размещений. Идемпотентен: сходившие даты
    пропускаются (одна дата = один запрос ISS на каждый борд размещения)."""
    from services import primary_placements as pp
    return await pp.backfill(days=days, force=force)
