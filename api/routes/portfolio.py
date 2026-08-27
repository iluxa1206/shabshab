"""Вкладка ПОРТФЕЛЬ: сборка набора флоатеров по фильтру и сумме.

Ничего не хранит — конструктор считает набор на каждый запрос, состояние
ручной правки (exclude/pin/manual) живёт в UI и приезжает в теле. Identity —
cookie-сессия, как у сигналов: расчёт тяжёлый, анонимам он не нужен.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from api.routes.auth import require_user
from services import portfolio_build
from services.screener_core import FilterError

router = APIRouter()


class PortfolioParams(BaseModel):
    # отбор бумаг — те же поля, что у фильтра СИГНАЛОВ (считает static_candidates)
    ratings: List[str] = []
    emitters: List[str] = []
    isins: List[str] = []
    issuer: str = "all"                    # all | ofz | corp
    years_min: Optional[float] = None
    years_max: Optional[float] = None
    hide_subord: bool = True
    spread_min: Optional[float] = None     # Y-IDX, бп
    spread_max: Optional[float] = None
    # портфельные фильтры
    bases: List[str] = []                  # KEYRATE | RUONIA, пусто = любая
    no_amort: bool = False
    no_call: bool = False
    min_adv_rub: Optional[float] = None    # средний дневной оборот за 30 дней
    # сборка
    mode: str = "spread"                   # spread | ladder
    buckets: Optional[List[float]] = None  # границы корзин лесенки, лет
    n: int = 15
    amount_rub: float = 100_000_000.0
    max_per_emitter: int = 1
    max_emitter_share: Optional[float] = None
    max_rating_share: Optional[float] = None
    # ручная правка набора
    exclude: List[str] = []
    pin: List[str] = []
    manual: Dict[str, float] = {}


@router.post("/build")
async def build_portfolio(params: PortfolioParams = Body(...),
                          user=Depends(require_user)):
    try:
        return await portfolio_build.build_live(params.model_dump(exclude_none=True))
    except FilterError as e:
        raise HTTPException(status_code=400, detail=str(e))
