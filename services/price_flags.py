"""Признак «цена в строке — не сегодняшняя сделка» (price_stale), одно правило.

Раньше флаг ставился только при last=None. Но Alor присылает в КАЖДОЙ
котировке цену последней сделки, в том числе вчерашней, а стрим складывает её
в кэш цен с меткой «сейчас» (universe_stream._seed_price): так в строку
попадал prev-close под видом цены текущей сессии без пометки. Замер на проде
23.09.2026: у 64–75 фиксов и 173–191 флоатеров цена = prev-close без сделки
сегодня, а price_stale=False.
"""
from typing import Optional

_EPS = 1e-9


def price_is_stale(last: Optional[float], prev: Optional[float],
                   traded_today: Optional[bool]) -> bool:
    """True — показана вчерашняя цена, сегодня сделок не было.

    traded_today: был ли сегодня оборот (биржевой VALTODAY ИЛИ свой счёт по
    тикам Alor); None — не знаем, тогда правило молчит (прежнее поведение).
    Условие «last == prev» нарочно узкое: цена, отличная от вчерашней, не может
    быть вчерашней, даже если оборот в снапшоте ещё не обновился."""
    if prev is None:
        return False
    if last is None:
        return True
    return traded_today is False and abs(last - prev) < _EPS
