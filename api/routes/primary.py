"""Витрина анонсов первички (services/primary_calendar)."""
from fastapi import APIRouter

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
