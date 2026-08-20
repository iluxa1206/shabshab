from __future__ import annotations
from pathlib import Path
import requests
import asyncio
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
# Async-путь ходит с КОРОТКИМ таймаутом: токен нужен горячему циклу, и «ждём
# oauth 15 секунд» — это не отказоустойчивость, а стоп-кран для всего процесса.
ASYNC_TIMEOUT = float(os.getenv("ALOR_AUTH_TIMEOUT", "5"))

# get_access_token вызывается синхронно (через asyncio.to_thread) из нескольких
# корутин — сериализуем обновление токена, чтобы не плодить дубль-логины/rate-limit.
_token_lock = threading.Lock()

# Токен в ПАМЯТИ процесса: (token, expires_at). Файл остаётся вторым уровнем —
# он переживает рестарт, но читать и парсить его на каждый вызов незачем.
_mem: tuple[Optional[str], float] = (None, 0.0)
# Single-flight для async-пути: девять вызывающих не должны устроить девять
# логинов, когда токен протух.
_alock: Optional["asyncio.Lock"] = None


def _mem_token(now: float) -> Optional[str]:
    token, exp = _mem
    return token if token and exp - 30 > now else None


def _remember(token: str, expires_at: float) -> None:
    global _mem
    _mem = (token, expires_at)


def _read_cached_token(now: float) -> Optional[str]:
    if not TOKEN_CACHE_FILE.exists():
        return None
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        token = cache.get("access_token")
        exp = cache.get("expires_at", 0)
        if token and exp - 30 > now:  # -30 чтобы с запасом
            _remember(token, exp)
            return token
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_token(token: str, expires_at: float) -> None:
    """Файловый кэш: атомарная замена, ошибки записи не фатальны (память уже
    обновлена, а файл нужен только чтобы пережить рестарт процесса)."""
    _remember(token, expires_at)
    tmp = TOKEN_CACHE_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expires_at": expires_at},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, TOKEN_CACHE_FILE)
    except OSError:
        pass


async def alor_token() -> str:
    """Access-токен Alor для КОРУТИН — без потоков и без блокирующего requests.

    Почему не asyncio.to_thread(get_access_token): пул to_thread один на всё
    приложение (6 воркеров на двухъядерном хосте) и обслуживает ещё и чтения
    SQLite под запросы API. Токен просят девять мест; когда он протухал или
    oauth.alor.ru не отвечал, все шесть воркеров вставали в requests на 15
    секунд, и весь сайт замирал (в проде ловилось сторожем лага: «потоки в
    момент лага: 6× auth.py:get_access_token», лаг 4.4с).

    Здесь: сначала память, потом файл (переживает рестарт), и только потом сеть
    — одним запросом под asyncio.Lock, чтобы девять ждущих дали один логин."""
    global _alock
    now = time.time()
    token = _mem_token(now) or _read_cached_token(now)
    if token:
        return token
    if _alock is None:
        _alock = asyncio.Lock()
    async with _alock:
        now = time.time()
        token = _mem_token(now) or _read_cached_token(now)
        if token:                      # обновил тот, кто дошёл до сети первым
            return token
        timeout = aiohttp.ClientTimeout(total=ASYNC_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(AUTH_URL, params={"token": REFRESH_TOKEN}) as r:
                r.raise_for_status()
                data = await r.json()
        token = data["AccessToken"]
        logger.info("Alor access token refreshed (async)")
        _save_token(token, now + TOKEN_TTL)
        return token


def get_access_token(refresh_token: str) -> str:
    """Синхронный путь — для скриптов и не-async кода. В корутинах используйте
    alor_token(): этот вызов блокирует поток пула до HTTP_TIMEOUT."""
    now = time.time()
    token = _mem_token(now) or _read_cached_token(now)
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
        _save_token(token, now + TOKEN_TTL)
        return token
