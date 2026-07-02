from fastapi import APIRouter
from datetime import datetime, timezone
from api.schemas import HealthResponse

router = APIRouter()

@router.get("/health", response_model=HealthResponse, tags=["System"])
async def get_health():
    return HealthResponse(
        status="OK",
        version="1.1",
        time=datetime.now(timezone.utc)
    )
