from __future__ import annotations
from pathlib import Path
import requests
import os, time, json, threading, logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import aiohttp


from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

REFRESH_TOKEN = os.getenv("ALOR_REFRESH_TOKEN")
AUTH_URL = "https://oauth.alor.ru/refresh"
BASE_API = "https://api.alor.ru"

from services.paths import cache_path as _cache_path
TOKEN_CACHE_FILE = Path(_cache_path("alor_token_cache.json"))
TOKEN_TTL = 28 * 60 # 28 минут
HTTP_TIMEOUT = 15.0

# get_access_token вызывается синхронно (через asyncio.to_thread) из нескольких
# корутин — сериализуем обновление токена, чтобы не плодить дубль-логины/rate-limit.
_token_lock = threading.Lock()


def _read_cached_token(now: float) -> Optional[str]:
    if not TOKEN_CACHE_FILE.exists():
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        token = cache.get("access_token")
        if token and cache.get("expires_at", 0) - 30 > now:  # -30 чтобы с запасом
            return token
    except (json.JSONDecodeError, OSError):
        pass
    return None


def get_access_token(refresh_token: str) -> str:
    token = _read_cached_token(time.time())
    if token:
        return token

    with _token_lock:
        # повторная проверка под локом — другой поток мог уже обновить
        now = time.time()
        token = _read_cached_token(now)
        if token:
            return token

        resp = requests.post(AUTH_URL, params={"token": refresh_token}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        token = data["AccessToken"]
        logger.info("Alor access token refreshed")

        tmp = TOKEN_CACHE_FILE.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"access_token": token, "expires_at": now + TOKEN_TTL},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, TOKEN_CACHE_FILE)  # атомарная замена
        except OSError:
            pass

        return token
