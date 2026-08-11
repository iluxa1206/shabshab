"""Валидация initData Telegram Mini App (спека Web Apps).

Алгоритм Telegram:
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  data_check_string = отсортированные "key=value" всех полей кроме hash, через \n
  ожидаемый hash = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()

Сравнение — constant-time (hmac.compare_digest). Свежесть auth_date проверяется,
иначе перехваченный initData валиден вечно."""
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

MAX_AGE_SEC = 24 * 3600     # Telegram выдаёт initData на открытие Mini App


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, *, max_age_sec: int = MAX_AGE_SEC) -> dict:
    """→ {"tg_user_id", "username", "first_name"} из подписанного initData.
    Бросает InitDataError на любой дефект: подпись, свежесть, отсутствие user."""
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        raise InitDataError("TG_BOT_TOKEN не задан")
    if not init_data or len(init_data) > 8192:
        raise InitDataError("пустой или слишком длинный initData")

    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)
    got_hash = data.pop("hash", None)
    if not got_hash:
        raise InitDataError("нет hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, got_hash):
        raise InitDataError("подпись не сошлась")

    try:
        auth_date = int(data.get("auth_date") or 0)
    except ValueError:
        raise InitDataError("auth_date не число")
    if auth_date <= 0 or time.time() - auth_date > max_age_sec:
        raise InitDataError("initData протух")

    try:
        user = json.loads(data.get("user") or "")
    except (ValueError, TypeError):
        raise InitDataError("нет user")
    uid = user.get("id")
    if not isinstance(uid, int):
        raise InitDataError("нет user.id")
    return {"tg_user_id": uid, "username": user.get("username"),
            "first_name": user.get("first_name")}


def sign_init_data(fields: dict, token: Optional[str] = None) -> str:
    """Собирает подписанный initData из полей (значения — строки). Для тестов."""
    from urllib.parse import urlencode
    token = token or os.getenv("TG_BOT_TOKEN") or ""
    data = {k: str(v) for k, v in fields.items()}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)
