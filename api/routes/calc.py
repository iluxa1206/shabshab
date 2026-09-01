"""Вкладка КАЛЬКУЛЯТОР — расчёт кастомной (не обращающейся/новой) облигации по
введённым параметрам. Сама математика живёт в services/custom_bond: тот же
модуль зовут анонсы первички, поэтому цифры двух витрин сходятся по построению,
а не по совпадению. Сравнение с рынком фронт строит сам по /api/fixed."""
from fastapi import APIRouter, Query

from services import custom_bond

router = APIRouter()


@router.get("/custom_floater", tags=["Calc"])
async def calc_custom_floater(
    base: str = Query(..., description="База: KEYRATE | RUONIA"),
    spread_bps: float = Query(..., ge=-1000, le=10000, description="Спред к базе, bps"),
    freq: int = Query(..., description="Выплат в год: 1/2/4/12"),
    maturity: str = Query(..., description="Дата погашения, ISO"),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
    face: float = Query(1000.0, gt=0, description="Номинал"),
):
    """Метрики кастомного ФЛОАТЕРА (база + спред): Y-IDX/SM/DM/YTM тем же
    прод-путём, что строки таблицы флоатеров."""
    return await custom_bond.price_floater(base, spread_bps, freq, maturity, price, face)


@router.get("/custom", tags=["Calc"])
async def calc_custom(
    coupon_pct: float = Query(..., ge=0, le=100, description="Ставка купона, % годовых"),
    freq: int = Query(..., description="Выплат в год: 1/2/4/12"),
    maturity: str = Query(..., description="Дата погашения, ISO"),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
    face: float = Query(1000.0, gt=0, description="Номинал"),
):
    """Метрики кастомного ФИКСА: YTM/тек.дох/G-спред/Z-спред/дюрации/DV01."""
    return await custom_bond.price_fixed(coupon_pct, freq, maturity, price, face)
