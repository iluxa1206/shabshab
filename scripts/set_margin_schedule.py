#!/usr/bin/env python3
"""Завести/снять РУЧНУЮ лесенку маржи по номерам купонов.

    python3 scripts/set_margin_schedule.py RU000A0JU9K4 "7-20=400"
    python3 scripts/set_margin_schedule.py RU000A0JU9K4 ""     # снять
    python3 scripts/set_margin_schedule.py RU000A0JU9K4        # показать

Нужен там, где парсер проспекта молчит СОЗНАТЕЛЬНО — ранние ступени стоят на
другой базе, и лесенка из текста была бы ложной. Ситиматик RU000A0JU9K4:
«1 купон — 11%, 2-6 — MAX(инфляция+4%; ставка рефинансирования+1%), 7-20 — КС+4%»:
к КС относится только хвост, его и заводим («7-20=400»).

Купоны ВНЕ диапазонов лесенки трактуются как не плавающие: прайсинг берёт по
ним скаляр margin_bps, бэктест спеки их не судит (services/bond_audit._backtest).

То же самое делается руками на странице СПРАВОЧНИК (поле «Лесенка маржи»);
скрипт — для прода, где правку удобнее прогнать без UI. Сразу после записи
пересчитывает бэктест спеки и кладёт вердикт в реестр.
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import instruments_registry as reg          # noqa: E402
from services.coupon_calib import parse_margin_schedule_field  # noqa: E402


async def _recheck(isin: str) -> None:
    """Пересчитать бэктест спеки тем же ядром, что дневной синк (шаг 8)."""
    from services.market_data import MarketDataService
    from services.bond_audit import _backtest
    from services.ref_data import coupon_formula

    row = reg.get(isin) or {}
    base = row.get("base")
    if base not in ("KEYRATE", "RUONIA"):
        print(f"{isin}: base={base} — бэктест не применим")
        return
    full = await MarketDataService.fetch_bond_schedule_full(isin)
    coupons = (full or {}).get("coupons") or []
    amorts = (full or {}).get("amorts") or []
    if not coupons:
        print(f"{isin}: расписание MOEX пустое — бэктест пропущен")
        return
    today = date.today()
    margin_pct = (row.get("margin_bps") or 0) / 100.0
    face = row.get("face_value") or 1000.0
    spec = coupon_formula(isin, coupons=coupons, margin_pct=margin_pct,
                          face=face, calc_date=today, amorts=amorts)
    bt = _backtest(isin, base, spec, coupons, margin_pct, face, today, amorts)
    err = bt.get("med_err_pp", bt.get("mean_err_pp"))
    reg.set_spec_backtest(isin, err, bt.get("verdict") or "NO_DATA", bt.get("n") or 0)
    print(f"{isin}: бэктест {bt.get('verdict')} err={err} на {bt.get('n')} купонах")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    isin = sys.argv[1].strip().upper()
    if reg.get(isin) is None:
        print(f"{isin}: нет в реестре")
        return 1
    if len(sys.argv) == 2:
        print(f"{isin}: margin_schedule = {reg.get(isin)['margin_schedule']!r}")
        return 0

    raw = sys.argv[2]
    try:
        steps = parse_margin_schedule_field(raw)
    except ValueError as e:
        print(f"лесенка не принята: {e}")
        return 2
    value = "; ".join(f"{s['from']}-{s['to']}={s['bps']}" for s in (steps or []))
    # lock=False СОЗНАТЕЛЬНО: margin_schedule читается реестровым фолбэком для
    # всех строк, и manual_locked=1 ради одного поля заморозил бы строку целиком
    # (freeze-trap импорта xlsx — см. scripts/unfreeze_fixing_spec.py).
    reg.set_manual(isin, {"margin_schedule": value}, lock=False)
    reg.invalidate_params_cache()
    print(f"{isin}: margin_schedule = {value!r}")
    asyncio.run(_recheck(isin))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
