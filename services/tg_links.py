"""Ссылки на дашборд для сообщений бота.

Одно место на весь телеграм-слой: в чате ссылка — единственный способ уйти из
карточки в приложение, и собирать её строкой по месту значит рано или поздно
разойтись с реальным путём SPA.

TG_SITE_URL — корень площадки (на проде https://assetallocator.ru/desk/), а
само приложение живёт под /app: react-router смонтирован с basename
`<префикс>/app` (см. api.js APP_BASENAME и SPA-fallback в api/main.py), и
ссылка без /app упирается в редирект, теряя маршрут."""
from __future__ import annotations

import os

DEFAULT_SITE = "https://assetallocator.ru/desk/"


def site() -> str:
    """Корень площадки со слэшем на конце."""
    return (os.getenv("TG_SITE_URL") or DEFAULT_SITE).rstrip("/") + "/"


def page(path: str = "") -> str:
    """Ссылка на страницу приложения: page('signals') → …/app/signals."""
    return site() + "app/" + path.lstrip("/")


def bond(isin: str) -> str:
    """График выпуска — куда идут из дайджеста, увидев движение премии."""
    return page(f"chart/{isin}")
