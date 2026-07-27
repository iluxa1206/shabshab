"""CRUD алертов по стакану, per-user (identity из cookie-сессии). Мониторинг —
фоновый воркер api.main.alerts_monitor."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from api.routes.auth import require_user
from services import alerts

router = APIRouter()


class AlertCreate(BaseModel):
    isin: str
    side: str            # buy | sell
    metric: str          # price | ytm | dm | gspread
    op: str              # '<=' | '>='
    threshold: float
    min_volume: float = 0.0
    volume_unit: str = "bonds"   # bonds | rub
    kind: str = "floater"        # floater | fixed
    note: Optional[str] = None


@router.get("", tags=["Alerts"])
async def list_alerts(user: dict = Depends(require_user)):
    return {"alerts": alerts.list_for_user(user["email"])}


@router.post("", tags=["Alerts"])
async def create_alert(body: AlertCreate, user: dict = Depends(require_user)):
    try:
        a = alerts.create(
            user["email"], isin=body.isin, side=body.side, metric=body.metric,
            op=body.op, threshold=body.threshold, min_volume=body.min_volume,
            volume_unit=body.volume_unit, kind=body.kind, note=body.note)
    except alerts.AlertError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return a


@router.delete("/{aid}", tags=["Alerts"])
async def remove_alert(aid: int = Path(...), user: dict = Depends(require_user)):
    """Активный → отмена (soft, остаётся в истории). Иначе → полное удаление."""
    a = alerts.get(aid)
    if not a or a["user_email"] != user["email"]:
        raise HTTPException(status_code=404, detail="Алерт не найден")
    if a["status"] == "active":
        alerts.cancel(user["email"], aid)
        return {"status": "cancelled"}
    alerts.delete(user["email"], aid)
    return {"status": "deleted"}
