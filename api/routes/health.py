from services import telegram
from fastapi import APIRouter
from datetime import datetime, timezone
from api.schemas import HealthResponse

router = APIRouter()

@router.get("/health", response_model=HealthResponse, tags=["System"])
async def get_health():
    return HealthResponse(
        status="OK",
        version="1.1",
        time=datetime.now(timezone.utc),
        # off | ok | down — внешний пингер (UptimeRobot и т.п.) ловит слово
        # «down» там, где сам Telegram о своей поломке сообщить не может
        telegram=telegram.channel_status()["state"],
    )
