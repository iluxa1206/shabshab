"""2-й источник кредитных рейтингов — smart-lab.ru (фолбэк к corpbonds).

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
