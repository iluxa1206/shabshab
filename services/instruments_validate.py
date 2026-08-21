"""Самопроверка данных реестра против авторитетных источников (MOEX+ЦБ).

Для каждой прайсуемой бумаги:
  • 0 будущих незафиксированных купонов → это ФИКС-бумага, ошибочно как флоатер
    (Cbonds иногда так метит) → reclassify base='FIXED', уходит из универса;
  • бэк-аут маржи из последнего зафикс. купона (ставка − маржа) сверяется с
    фактическим КС/RUONIA на дату фиксинга; |Δ|>1.5pp → suspect (вероятно неверная
    маржа/база из Cbonds) → записываем margin_check_pp, бумага всплывает в ревью;
  • ПЕРЕСМОТР НА ОФЕРТЕ: маржа по купонам до и после прошедшей оферты. Сдвиг
    ≥25bps значит, что эмитент этот выпуск пересматривает, и к следующей оферте
    поток надо резать — а решает это var_type из Cbonds, которого нет у 115 из
    134 бумаг с офертой. Флаг НИЧЕГО НЕ МЕНЯЕТ В РАСЧЁТЕ: бумага попадает в
    ревью, резать или нет — решение человека в Справочнике.

Расписание берётся из дневного кэша (fetch_bond_schedule_full), так что после
прогрева поллера сеть почти не задействуется. Инвариант «расчёт верен» —
самопроверяемый: плохой параметр ловится сверкой с реальными выплатами.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

logger = logging.getLogger(__name__)

_SUSPECT_PP = 1.5


def _d(s):
    from datetime import date as _dt
    try:
        return _dt.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _last_fixed(coupons, calc_date):
    best = None
    for c in coupons or []:
        s, e, v, vp = _d(c.get("start")), _d(c.get("end")), c.get("value"), c.get("valueprc")
        if not s or not e or v is None or e > calc_date:
            continue
        if best is None or e > best[0]:
            best = (e, s, float(vp) if vp else None, float(v))
    return best


def _index_at(hist, d):
    val = None
    for md, r in hist:
        if md <= d:
            val = r
        else:
            break
    return val


def _margin_bps(c, face, hist) -> "float | None":
    """Фактическая маржа зафиксированного купона: ставка − индекс на старте, bps."""
    s, e, v, vp = _d(c.get("start")), _d(c.get("end")), c.get("value"), c.get("valueprc")
    if not s or not e or (v is None and vp is None):
        return None
    days = (e - s).days or 1
    rate = float(vp) if vp else float(v) * 365.0 / (days * (face or 1000)) * 100.0
    idx = _index_at(hist, s)
    return None if idx is None else (rate - idx) * 100.0


def _offer_reset(isin, coupons, offers, face, hist, calc_date) -> bool:
    """Сравнивает маржу до и после ПОСЛЕДНЕЙ прошедшей оферты. Пишет находку в
    реестр (флаг ревью, не расчёт). True — сдвиг больше порога.

    Берём по 3 купона с каждой стороны и усредняем: один купон может отличаться
    из-за округления рублёвой суммы и короткого периода."""
    from services import instruments_registry as reg
    past, future = [], []
    for o in offers or []:
        d = _d((o.get("date") or "")[:10])
        if not d:
            continue
        (past if d < calc_date else future).append(d)
    next_offer = min(future).isoformat() if future else None
    if not past:
        reg.set_offer_reset(isin, None, None, next_offer)
        return False
    odate = max(past)

    def side(pred):
        vals = [_margin_bps(c, face, hist) for c in coupons if pred(c)]
        vals = [v for v in vals if v is not None]
        return vals

    before = side(lambda c: (_d(c.get("end")) or calc_date) <= odate)[-3:]
    after = side(lambda c: (_d(c.get("start")) or calc_date) >= odate
                 and (_d(c.get("end")) or calc_date) <= calc_date)[:3]
    if not before or not after:
        reg.set_offer_reset(isin, None, None, next_offer)
        return False
    delta = sum(after) / len(after) - sum(before) / len(before)
    reg.set_offer_reset(isin, delta, odate.isoformat(), next_offer)
    return abs(delta) >= reg.OFFER_RESET_BPS


async def validate_priceable() -> dict:
    """Прогон самопроверки по всем прайсуемым флоатерам. Возвращает статистику."""
    from services import instruments_registry as reg, cbr
    from services.market_data import MarketDataService

    calc_date = date.today()
    ks_hist = cbr.ks_history()
    ruo_hist = cbr.ruonia_history()
    rows = [reg.get(r["isin"]) for r in reg.universe_rows(only_priceable=True)]

    reclassified = suspect = checked = resets = 0
    sem = asyncio.Semaphore(6)

    async def one(row):
        nonlocal reclassified, suspect, checked, resets
        isin = row["isin"]
        base = row.get("base")
        margin = row.get("margin_bps")
        if base not in ("KEYRATE", "RUONIA") or margin is None:
            return
        async with sem:
            try:
                full = await MarketDataService.fetch_bond_schedule_full(isin)
            except Exception:
                return
        coupons = (full or {}).get("coupons") or []
        if not coupons:
            return
        # фикс-бумага (все купоны зафиксированы) — не флоатер
        if not any(c.get("value") is None for c in coupons):
            if not row.get("manual_locked"):
                reg.reclassify_fixed(isin)
                reclassified += 1
            return
        # бэк-аут маржи
        lf = _last_fixed(coupons, calc_date)
        if not lf:
            return
        end, start, valueprc, value = lf
        days = (end - start).days or 1
        # номинал ПЕРИОДА купона, не текущий: у амортизируемых бумаг value
        # платился на больший остаток — деление на текущий face завышало ставку
        # (БалтЛизП10: 13.71₽ на 1000₽ делили на 900₽ → ложный suspect +1.7пп).
        # _face_on откатывает face назад по траншам, выплаченным в (start, calc].
        from services.coupon_calib import _face_on
        face_cur = row.get("face_value") or 1000
        face = _face_on(face_cur, (full or {}).get("amorts"), start, calc_date)
        rate = valueprc if valueprc else (value * 365.0 / (days * face) * 100.0)
        hist = ks_hist if base == "KEYRATE" else ruo_hist
        actual = _index_at(hist, start)
        if actual is None:
            return
        diff = (rate - margin / 100.0) - actual
        reg.set_margin_check(isin, diff)
        checked += 1
        if abs(diff) > _SUSPECT_PP:
            suspect += 1

        # пересмотр на оферте — по тому же расписанию, без второго запроса
        try:
            if _offer_reset(isin, coupons, (full or {}).get("offers"),
                            row.get("face_value") or 1000, hist, calc_date):
                resets += 1
        except Exception as e:
            logger.debug("offer reset %s: %s", isin, e)

    await asyncio.gather(*(one(r) for r in rows if r))
    stats = {"checked": checked, "reclassified_fixed": reclassified,
             "suspect": suspect, "offer_resets": resets}
    logger.info("registry validation: %s", stats)
    return stats
