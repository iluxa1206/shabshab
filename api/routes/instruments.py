"""Admin-CRUD реестра инструментов (services.instruments_registry).

Ручной ввод/правка расчётных параметров новых бумаг + список «на ревью».
Всё под require_admin. Расчётные поля тут — то, что нужно нашему прайсингу без НРД.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from api.routes.auth import require_admin
from services import instruments_registry as reg

router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _require_isin(isin: str) -> str:
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=422, detail="Некорректный ISIN")
    return isin


class InstrumentParams(BaseModel):
    """Ручные параметры бумаги. Все опциональны — правим только заданные поля."""
    base: Optional[str] = Field(None, description="KEYRATE | RUONIA | FIXED")
    margin_bps: Optional[int] = None
    maturity_date: Optional[str] = Field(None, description="ISO YYYY-MM-DD")
    issue_date: Optional[str] = None
    coupon_period_days: Optional[int] = Field(None, ge=1, le=1830)
    coupons_per_year: Optional[int] = Field(None, ge=1, le=365)
    day_count: Optional[str] = None
    face_value: Optional[float] = Field(None, gt=0)
    fixing_lag: Optional[int] = Field(None, ge=0, le=30)
    fixing_lag_unit: Optional[str] = Field(None, description="cal | work")
    coupon_mode: Optional[str] = Field(None, description="point | average")
    short_name: Optional[str] = Field(None, max_length=128)
    var_type: Optional[str] = None


_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@router.get("/unreviewed", tags=["Instruments"])
async def unreviewed(_admin: dict = Depends(require_admin)):
    """Новые на ревью + непрайсуемые (нет base/margin/maturity) + suspect (маржа
    расходится с фактом КС/RUONIA) + счётчики."""
    return {"items": reg.list_unreviewed(),
            "incomplete": reg.list_incomplete(),
            "suspect": reg.list_suspect(),
            "count": reg.count()}


@router.get("/{isin}", tags=["Instruments"])
async def get_instrument(isin: str = Path(...), _admin: dict = Depends(require_admin)):
    isin = _require_isin(isin)
    row = reg.get(isin)
    if row is None:
        raise HTTPException(status_code=404, detail="Нет в реестре")
    return row


@router.post("/{isin}", tags=["Instruments"])
async def set_instrument(body: InstrumentParams, isin: str = Path(...),
                         _admin: dict = Depends(require_admin)):
    """Ручной ввод/правка параметров (lock — sync их впредь не затрёт) + reviewed."""
    isin = _require_isin(isin)
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    if not params:
        raise HTTPException(status_code=422, detail="Нет полей для сохранения")
    if body.base is not None and body.base not in ("KEYRATE", "RUONIA", "FIXED"):
        raise HTTPException(status_code=422, detail="base ∈ KEYRATE|RUONIA|FIXED")
    for f in ("maturity_date", "issue_date"):
        if params.get(f) and not _ISO_RE.fullmatch(params[f]):
            raise HTTPException(status_code=422, detail=f"{f}: ожидается YYYY-MM-DD")
    if body.fixing_lag_unit is not None and body.fixing_lag_unit not in ("cal", "work"):
        raise HTTPException(status_code=422, detail="fixing_lag_unit ∈ cal|work")
    if body.coupon_mode is not None and body.coupon_mode not in ("point", "average"):
        raise HTTPException(status_code=422, detail="coupon_mode ∈ point|average")
    reg.set_manual(isin, params, lock=True)
    return {"ok": True, "instrument": reg.get(isin)}


@router.post("/{isin}/reviewed", tags=["Instruments"])
async def mark_reviewed(isin: str = Path(...), _admin: dict = Depends(require_admin)):
    """Пометить бумагу проверенной без правок (параметры из авто-sync устраивают)."""
    isin = _require_isin(isin)
    if reg.get(isin) is None:
        raise HTTPException(status_code=404, detail="Нет в реестре")
    reg.mark_reviewed(isin)
    return {"ok": True}
