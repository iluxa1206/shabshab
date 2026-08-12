"""smart-lab.ru: кредитный рейтинг (фолбэк к corpbonds) и ТИП КУПОНА для аудита.

corpbonds не индексирует свежие/мелкие выпуски (ВДО 2025) → большинство фиксов
были NR. smart-lab показывает агрегированный «Кредитный рейтинг» бакетом прямо
в статике страницы (linear-progress-bar__text) — покрытие ~100% на тесте.
Используется в services.ratings.refresh при промахе corpbonds."""
import re
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; floaters-desk/1.0)"}
# Тип купона словами в заголовке страницы: «Облигация … с фиксированным купоном».
# Источник, НИКАК не связанный с нашим расчётом, — тем и ценен для сверки.
_RE_FIX = re.compile(r"с\s+фиксированным\s+купоном", re.I)
_RE_FLT = re.compile(r"с\s+(?:плавающим|переменным)\s+купоном", re.I)
# «Кредитный рейтинг» → ближайший linear-progress-bar__text с латинским грейдом
_RE = re.compile(r"Кредитный рейтинг.*?linear-progress-bar__text[^>]*>\s*([A-D]{1,3}[+-]?)\s*<",
                 re.S)


async def fetch_smartlab_rating(isin: str, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """Грейд рейтинга с smart-lab («AAA»/«BBB+»/…) или None. Латинский бакет,
    маппится ratings.rating_to_bucket (суффиксы +/- отбрасываются)."""
    url = f"https://smart-lab.ru/q/bonds/{isin}/"
    try:
        if client is None:
            async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
                resp = await c.get(url)
        else:
            resp = await client.get(url, headers=_UA, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return None
        m = _RE.search(resp.text)
        return m.group(1) if m else None
    except Exception as e:
        logger.debug(f"smartlab rating {isin}: {e}")
        return None


async def fetch_smartlab_coupon_type(isin: str,
                                     client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """'fixed' | 'floater' | None — тип купона по формулировке smart-lab.

    None = сайт про тип молчит (у части выпусков шаблон без этой фразы) либо
    страница недоступна. Молчание НЕ трактуем: «не знаем» и «фикс» — разные
    вещи, иначе сверка сама начнёт плодить ложные расхождения."""
    url = f"https://smart-lab.ru/q/bonds/{isin}/"
    try:
        if client is None:
            async with httpx.AsyncClient(headers=_UA, timeout=20, follow_redirects=True) as c:
                resp = await c.get(url)
        else:
            resp = await client.get(url, headers=_UA, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return None
        # тип стоит в заголовке — хвост страницы (таблицы, скрипты) не читаем
        head = resp.text[:20000]
        if _RE_FLT.search(head):
            return "floater"
        if _RE_FIX.search(head):
            return "fixed"
        return None
    except Exception as e:
        logger.debug(f"smartlab coupon type {isin}: {e}")
        return None
