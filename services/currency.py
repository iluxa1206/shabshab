"""Валюта номинала облигации — ОДНО правило нормализации на весь бэк.

MOEX отдаёт FACEUNIT как есть: пусто/SUR/RUR — рубль, CNH — тот же юань, что CNY.
Раньше правило жило копиями (fixed.py, bond_details, universe_stream…), и в
карточке флоатера SUR оставался SUR, а CNH не склеивался с CNY на бэке."""
from typing import Optional

RUB = "RUB"
_RUBLE_CODES = {"", "RUB", "SUR", "RUR"}
_ALIAS = {"CNH": "CNY"}


def normalize_ccy(unit: Optional[str]) -> str:
    """Код валюты номинала: RUB для пустого и старых кодов рубля, CNH → CNY."""
    u = (unit or "").strip().upper()
    if u in _RUBLE_CODES:
        return RUB
    return _ALIAS.get(u, u)


def is_ruble(unit: Optional[str]) -> bool:
    return normalize_ccy(unit) == RUB


def to_rub(amount: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Сумма в валюте номинала → рубли по последнему известному курсу.
    Рубль — как есть; курса нет — None (вместо суммы в чужой валюте)."""
    if amount is None:
        return None
    if is_ruble(unit):
        return amount
    from services.fx import cached_rate
    rate = cached_rate(normalize_ccy(unit))
    return float(amount) * rate if rate else None
