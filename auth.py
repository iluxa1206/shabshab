from __future__ import annotations
from pathlib import Path
import requests
import os, time, json
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import aiohttp


from dotenv import load_dotenv

load_dotenv()

REFRESH_TOKEN = os.getenv("ALOR_REFRESH_TOKEN")
AUTH_URL = "https://oauth.alor.ru/refresh"
BASE_API = "https://api.alor.ru"

TOKEN_CACHE_FILE = Path("alor_token_cache.json")
TOKEN_TTL = 28 * 60 # 28 минут

def get_access_token(refresh_token: str) -> str:
    now = time.time()

    if TOKEN_CACHE_FILE.exists():
        try:
            with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            token = cache.get("access_token")
            expires_at = cache.get("expires_at", 0)

            # если токен ещё жив — вернём его
            if token and expires_at - 30 > now:  # -30 чтобы с запасом
                return token
        except (json.JSONDecodeError, OSError):
            # если кэш сломан — просто пойдём за новым
            pass

    # 2. запрашиваем новый токен
    resp = requests.post(AUTH_URL, params={"token": refresh_token})
    resp.raise_for_status()
    data = resp.json()
    token = data["AccessToken"]
    print('token update')
    # 3. сохраняем в кэш
    to_cache = {
        "access_token": token,
        "expires_at": now + TOKEN_TTL,
    }
    with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(to_cache, f, ensure_ascii=False, indent=2)

    return token
