"""Бэктест спеки фиксинга по ФАКТУ выплат — для всего универса.

Для каждой бумаги пересчитывает прошлые ЗАФИКСИРОВАННЫЕ купоны нашей
эффективной спекой (ref_data.coupon_formula → projected_ks_pct на реальной
истории КС/RUONIA) и сравнивает со ставкой, которую эмитент реально заплатил.
Средняя |ошибка| в пп → вердикт: OK < 0.15, WARN < 0.5, BAD иначе.

Ошибка ≈ 0 доказывает, что лаг/окно/режим/маржа согласованы с выплатами;
систематика — признак неверно заполненных параметров в Справочнике.

Ядро расчёта — общее с паспортом бумаги (services.bond_audit._backtest),
чтобы вкладка и фильтр не разъезжались. Результат пишется в реестр
(spec_verdict/spec_err_pp), фильтр «спека расходится» читает его оттуда.

Зовётся из ежедневного синка (instruments_sync, шаг 8).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# сколько бумаг обрабатывать за проход синка (сеть MOEX: bondization на ISIN).
# Расписания берутся из дневного кэша (fetch_bond_schedule_full) — по большей
# части бесплатно, но холодный кэш растянет проход.
_BATCH = 24
_CONCURRENCY = 4


async def _one(isin: str, row: dict, today: date) -> dict | None:
    from services.market_data import MarketDataService
    from services.bond_audit import _backtest
    from services.ref_data import coupon_formula

    base = row.get("base")
    if base not in ("KEYRATE", "RUONIA"):
        return None
    try:
        full = await MarketDataService.fetch_bond_schedule_full(isin)
    except Exception as e:
        logger.debug("spec-backtest %s: расписание недоступно (%s)", isin, e)
        return None
    coupons = (full or {}).get("coupons") or []
    amorts = (full or {}).get("amorts") or []
    if not coupons:
        return {"isin": isin, "verdict": "NO_DATA", "err": None, "n": 0}
    margin_pct = (row.get("margin_bps") or 0) / 100.0
    face = row.get("face_value") or 1000.0
    try:
        spec = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct,
                              face=face, calc_date=today, amorts=amorts)
        bt = _backtest(isin, base, spec, coupons, margin_pct, face, today, amorts)
    except Exception as e:
        logger.debug("spec-backtest %s: %s", isin, e)
        return None
    # в реестр пишем МЕДИАННУЮ ошибку — тот же показатель, по которому вынесен
    # вердикт (среднее задирает разовый выброс битого купона)
    return {"isin": isin, "verdict": bt.get("verdict") or "NO_DATA",
            "err": bt.get("med_err_pp", bt.get("mean_err_pp")), "n": bt.get("n") or 0}


async def _recent_offer_isins(window_days: int = 45) -> set:
    """Бумаги, у которых оферта прошла в последние window_days.

    После оферты эмитент переставляет ставку — это ЕДИНСТВЕННЫЙ момент, когда
    спека меняется предсказуемо, и ловить его надо сразу, а не через месяц
    общей очереди."""
    from services import instruments_registry as reg
    from services.market_data import MarketDataService
    today = date.today()
    lo = (today - timedelta(days=window_days)).isoformat()
    out = set()
    for r in reg.universe_rows(only_priceable=True):
        isin = r["isin"]
        try:
            full = await MarketDataService.fetch_bond_schedule_full(isin)
        except Exception:
            continue
        for o in (full.get("offers") or []):
            d = str(o.get("date") or o.get("offerdate") or "")[:10]
            if d and lo <= d <= today.isoformat():
                out.add(isin)
                break
    if out:
        logger.info("spec backtest: в голову очереди %d бумаг после оферты", len(out))
    return out


async def run(limit: int = _BATCH, only_stale: bool = True) -> dict:
    """Прогнать бэктест по порции бумаг и записать вердикты в реестр.

    only_stale=True — сначала те, что ещё ни разу не проверены, затем самые
    давние: за несколько дней синка покрывается весь универс, а нагрузка на
    MOEX размазана.
    """
    from services import instruments_registry as reg

    rows = {r["isin"]: r for r in reg.universe_rows(only_priceable=True)}
    fresh_offer = await _recent_offer_isins()
    with_meta = []
    for isin in rows:
        full_row = reg.get(isin) or {}
        # ПРИОРИТЕТ БУМАГАМ ПОСЛЕ ОФЕРТЫ. Очередь по давности проверки обходит
        # универс за ~25 дней (24 бумаги за проход на 600), а ставка меняется
        # предсказуемо именно на оферте: эмитент ставит новый купон, и до своей
        # очереди бумага считается по старой спеке. Ставим их в голову.
        prio = "0" if isin in fresh_offer else "1"
        with_meta.append((prio + (full_row.get("spec_checked_at") or ""), isin, full_row))
    if only_stale:
        with_meta.sort(key=lambda x: x[0])          # "" (не проверенные) первыми
    todo = with_meta[:max(1, limit)]

    today = date.today()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _guarded(isin, row):
        async with sem:
            return await _one(isin, row, today)

    res = await asyncio.gather(*(_guarded(i, r) for _, i, r in todo),
                               return_exceptions=True)
    stats = {"checked": 0, "ok": 0, "warn": 0, "bad": 0, "no_data": 0}
    for r in res:
        if isinstance(r, Exception) or r is None:
            continue
        reg.set_spec_backtest(r["isin"], r["err"], r["verdict"], r["n"])
        stats["checked"] += 1
        stats[{"OK": "ok", "WARN": "warn", "BAD": "bad"}.get(r["verdict"], "no_data")] += 1
    return stats
