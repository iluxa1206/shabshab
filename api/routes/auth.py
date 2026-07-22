"""Авторизация дашборда: логин/пароль → JWT в httpOnly-cookie.

Доступ к данным закрыт зависимостью require_user (см. api/main.py). Аккаунты
создаёт админ через scripts/useradd.py — самостоятельной регистрации нет,
поэтому чужие аккаунты попасть не могут.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel

from services import auth_users

load_dotenv()  # AUTH_SECRET / AUTH_COOKIE_SECURE из .env

logger = logging.getLogger(__name__)
router = APIRouter()

# --- Rate-limit /login (SEC #2): in-memory backoff по ключу IP+email ---
# Одиночный процесс (uvicorn 1 воркер) — общий dict достаточен; при масштабировании
# на воркеры вынести в Redis. Экспоненциальный порог: чем больше неудач, тем дольше
# окно. Цель — сорвать онлайн-брутфорс единственного админ-пароля, не мешая юзеру.
_LOGIN_FAILS: dict[str, tuple[int, float]] = {}   # key -> (fail_count, window_start)
_LOGIN_MAX_FAILS = 5           # неудач до блокировки
_LOGIN_WINDOW_SEC = 300        # окно счётчика / базовая блокировка
_LOGIN_FAILS_MAX_ENTRIES = 4096  # защита самого dict от разрастания


def _login_key(request: Request, email: str) -> str:
    ip = request.client.host if request.client else "?"
    return f"{ip}|{(email or '').strip().lower()}"


def _login_blocked_for(key: str, now: float) -> float:
    """Секунд до разблокировки (0 = не заблокирован)."""
    ent = _LOGIN_FAILS.get(key)
    if not ent:
        return 0.0
    fails, start = ent
    if fails < _LOGIN_MAX_FAILS:
        return 0.0
    # блокировка растёт с числом неудач сверх порога (5→300с, 6→600с, capped 1ч)
    block = min(_LOGIN_WINDOW_SEC * (2 ** (fails - _LOGIN_MAX_FAILS)), 3600)
    remaining = start + block - now
    return remaining if remaining > 0 else 0.0


def _login_record_fail(key: str, now: float) -> None:
    if len(_LOGIN_FAILS) > _LOGIN_FAILS_MAX_ENTRIES:
        # выкидываем протухшие записи, чтобы dict не рос бесконечно (DoS-память)
        for k, (_f, s) in list(_LOGIN_FAILS.items()):
            if now - s > 3600:
                _LOGIN_FAILS.pop(k, None)
    fails, start = _LOGIN_FAILS.get(key, (0, now))
    if now - start > _LOGIN_WINDOW_SEC and fails < _LOGIN_MAX_FAILS:
        fails, start = 0, now      # окно истекло без блокировки — сброс
    _LOGIN_FAILS[key] = (fails + 1, start if fails else now)


def _login_reset(key: str) -> None:
    _LOGIN_FAILS.pop(key, None)

# AUTH_SECRET обязателен в проде — иначе токены нельзя подписать/проверить.
# Без него любой запрос к данным вернёт 401 (fail-closed), сайт остаётся закрыт.
SECRET = os.getenv("AUTH_SECRET", "")
ALGO = "HS256"
COOKIE_NAME = "session"
SESSION_TTL = timedelta(days=7)
# Secure-cookie обязателен под HTTPS (прод). Локально (http) выставить AUTH_COOKIE_SECURE=0.
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "1") not in ("0", "false", "False", "")


class LoginBody(BaseModel):
    email: str
    password: str


def _make_token(email: str, role: str, pwd_ver: str = "") -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": email, "role": role, "pv": pwd_ver,
               "iat": now, "exp": now + SESSION_TTL}
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def _decode_token(token: str) -> Optional[dict]:
    if not SECRET or not token:
        return None
    try:
        data = jwt.decode(token, SECRET, algorithms=[ALGO])
    except jwt.PyJWTError:
        return None
    email = data.get("sub")
    if not email:
        return None
    # сверяем с актуальным хранилищем — удалённый юзер сразу теряет доступ
    user = auth_users.get_user(email)
    if user is None:
        return None
    # версия пароля: смена пароля инвалидирует все выданные токены (в т.ч.
    # легаси-токены без pv — разовый ре-логин после этого деплоя)
    if data.get("pv") != user.get("pv"):
        return None
    return user


def _current_user_from_cookie(token: Optional[str]) -> Optional[dict]:
    return _decode_token(token) if token else None


def require_user(request: Request) -> dict:
    """FastAPI-зависимость: пускает только с валидной сессией, иначе 401."""
    user = _current_user_from_cookie(request.cookies.get(COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    """Только для роли admin (управление пользователями), иначе 403."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    return user


def user_from_websocket(websocket: WebSocket) -> Optional[dict]:
    """Проверка сессии для WS: cookie уходит на хендшейке (same-origin)."""
    return _current_user_from_cookie(websocket.cookies.get(COOKIE_NAME))


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class CreateUserBody(BaseModel):
    email: str
    password: str
    role: str = "user"


class UpdateUserBody(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not SECRET:
        raise HTTPException(status_code=503, detail="Авторизация не настроена (AUTH_SECRET)")
    now = time.time()
    key = _login_key(request, body.email)
    blocked = _login_blocked_for(key, now)
    if blocked > 0:
        logger.warning("login rate-limited: key=%s, %.0fs remaining", key, blocked)
        raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте позже.",
                            headers={"Retry-After": str(int(blocked) + 1)})
    # bcrypt блокирует event loop ~50-100мс → в threadpool (SEC #1)
    user = await asyncio.to_thread(auth_users.verify_credentials, body.email, body.password)
    if not user:
        _login_record_fail(key, now)
        logger.warning("login failed: key=%s", key)
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    _login_reset(key)
    token = _make_token(user["email"], user["role"], user.get("pv", ""))
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return {"email": user["email"], "role": user["role"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return {"email": user["email"], "role": user["role"]}


# --- Смена своего пароля (любой авторизованный) ---
@router.post("/password")
async def change_own_password(body: PasswordBody, user: dict = Depends(require_user)):
    # bcrypt в threadpool (SEC #1) — не блокируем event loop
    if not await asyncio.to_thread(auth_users.verify_credentials, user["email"], body.current_password):
        raise HTTPException(status_code=400, detail="Текущий пароль неверный")
    try:
        auth_users.set_password(user["email"], body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# --- Управление пользователями (только admin) ---
@router.get("/users")
async def list_users(_admin: dict = Depends(require_admin)):
    return {"users": auth_users.list_users()}


@router.post("/users")
async def create_user(body: CreateUserBody, _admin: dict = Depends(require_admin)):
    try:
        auth_users.add_user(body.email, body.password, role=body.role, overwrite=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.patch("/users/{email}")
async def update_user(email: str, body: UpdateUserBody, admin: dict = Depends(require_admin)):
    email = (email or "").strip().lower()
    if not auth_users.get_user(email):
        raise HTTPException(status_code=404, detail="Нет такого пользователя")
    # защита от lockout: нельзя разжаловать последнего админа
    if body.role is not None:
        if (body.role != "admin"
                and auth_users.get_user(email)["role"] == "admin"
                and auth_users.count_admins() <= 1):
            raise HTTPException(status_code=400, detail="Нельзя разжаловать последнего администратора")
        try:
            auth_users.set_role(email, body.role)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if body.password is not None:
        try:
            auth_users.set_password(email, body.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.delete("/users/{email}")
async def delete_user(email: str, admin: dict = Depends(require_admin)):
    email = (email or "").strip().lower()
    if email == admin["email"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить свой аккаунт")
    if not auth_users.get_user(email):
        raise HTTPException(status_code=404, detail="Нет такого пользователя")
    if (auth_users.get_user(email)["role"] == "admin"
            and auth_users.count_admins() <= 1):
        raise HTTPException(status_code=400, detail="Нельзя удалить последнего администратора")
    auth_users.remove_user(email)
    return {"ok": True}
