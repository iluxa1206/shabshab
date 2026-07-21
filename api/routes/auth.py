"""Авторизация дашборда: логин/пароль → JWT в httpOnly-cookie.

Доступ к данным закрыт зависимостью require_user (см. api/main.py). Аккаунты
создаёт админ через scripts/useradd.py — самостоятельной регистрации нет,
поэтому чужие аккаунты попасть не могут.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel

from services import auth_users

load_dotenv()  # AUTH_SECRET / AUTH_COOKIE_SECURE из .env

router = APIRouter()

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
async def login(body: LoginBody, response: Response):
    if not SECRET:
        raise HTTPException(status_code=503, detail="Авторизация не настроена (AUTH_SECRET)")
    user = auth_users.verify_credentials(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
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
    if not auth_users.verify_credentials(user["email"], body.current_password):
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
