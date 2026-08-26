#!/usr/bin/env python3
"""Детектор дней, записанных в архив баров БЕЗ спеки фиксинга.

Зачем. Спека купона (coupon_mode/lag/compounded) резолвится на лету из текста
проспекта. Если источник текста отваливается, projected_ks_pct молча уходит на
легаси форвард-проекцию кривой — это ДРУГАЯ методика, а не «менее точная»:
на ВЭБ2Р-50 разница 9 bps R-spread, на выборке доходит до 23 bps. Дыру в
источнике закрыл реестровый фолбэк (ref_data._registry_fallback), но строки
bar_daily, записанные ДО этого в деградированном окне, сами не чинятся:
прошлые дни пересчитываются as-of только пока не покрыты, а покрытая строка
текущей версии метрик не считается стейлом.

Что делает. Для выборки флоатеров сравнивает сохранённый y_idx с честным
пересчётом as-of ТЕКУЩИМ (починенным) кодом и печатает разбивку по датам.
Норма — медиана около нуля: расхождение отдельных бумаг бывает от тонких дней,
оферт и экзотики. ПОДОЗРИТЕЛЬНО — дата, где медиана уехала у МНОГИХ бумаг
разом: так выглядит день, посчитанный другой методикой.

Запускать НА ПРОДЕ (локальный бэкап portfolio.db бесполезен: в нём строки
перестроены одним батчем и следов прода не осталось).

    python scripts/verify_spec_drift.py --days 30 --limit 60

Лечение найденных дат — бамп services.bars.BARS_METRICS_VERSION: строки старой
версии зануляются и пересчитываются фоном честным as-of.
"""
import argparse
import asyncio
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from services.portfolio_db import DB_PATH as BARS_DB       # noqa: E402
from services.instruments_registry import DB_PATH as REG_DB  # noqa: E402
from services.backdate import asof_bar_metrics               # noqa: E402
from services.ref_data import coupon_formula                 # noqa: E402

# Медиана по бумагам за день. Порог намеренно ниже наблюдённого разрыва методик
# (9-23 bps): одиночные выбросы медиану не двигают, а системный сдвиг двигает.
SUSPECT_BPS = 3.0
MIN_BONDS = 5          # меньше — статистики на вывод не хватает

# Раньше этой даты as-of сравнивать НЕ с чем: архив своп-котировок копится с
# 2026-07-30, до него кривая дня реконструируется приближением и расхождение с
# записанным значением уходит в тысячи bps — это шум метода, а не наша дыра.
CURVE_ARCHIVE_FROM = "2026-07-30"


def _spec_bonds(limit: int) -> list[str]:
    con = sqlite3.connect(REG_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT isin FROM instruments WHERE active=1 "
                       "AND base IN ('RUONIA','KEYRATE')").fetchall()
    out = []
    for r in rows:
        try:
            if coupon_formula(r["isin"]).get("coupon_mode") is not None:
                out.append(r["isin"])
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


async def main(days: int, limit: int) -> int:
    con = sqlite3.connect(BARS_DB)
    con.row_factory = sqlite3.Row
    isins = _spec_bonds(limit)
    print(f"бумаг со спекой в выборке: {len(isins)}, окно {days} дн "
          f"(даты до {CURVE_ARCHIVE_FROM} пропускаются — нет архива кривой)")

    gaps = defaultdict(list)
    done = 0
    for isin in isins:
        rows = con.execute(
            "SELECT date, wap_pct, y_idx_wap_bps FROM bar_daily WHERE isin=? "
            "AND y_idx_wap_bps IS NOT NULL ORDER BY date DESC LIMIT ?",
            (isin, days)).fetchall()
        if not rows:
            continue
        try:
            fn = await asof_bar_metrics(isin, days + 5)
        except Exception:
            continue                     # бумага без as-of контекста — не наш случай
        done += 1
        for r in rows:
            if r["date"] < CURVE_ARCHIVE_FROM:
                continue
            y = (fn(r["date"], r["wap_pct"]) or {}).get("y_idx_bps")
            if y is not None:
                gaps[r["date"]].append(r["y_idx_wap_bps"] - y)

    print(f"пересчитано бумаг: {done}\n")
    print(f"{'дата':11s} {'бумаг':>6s} {'медиана Δ':>10s} {'мин':>8s} {'макс':>8s}")
    suspect = []
    for d in sorted(gaps, reverse=True):
        v = gaps[d]
        med = st.median(v)
        flag = ""
        if len(v) >= MIN_BONDS and abs(med) >= SUSPECT_BPS:
            flag = "  <-- ПОДОЗРИТЕЛЬНО"
            suspect.append((d, med, len(v)))
        print(f"{d:11s} {len(v):6d} {med:10.1f} {min(v):8.1f} {max(v):8.1f}{flag}")

    if suspect:
        print("\nдни, посчитанные похоже другой методикой:")
        for d, med, n in suspect:
            print(f"  {d}: медиана {med:+.1f} bps по {n} бумагам")
        print("\nлечение: поднять services.bars.BARS_METRICS_VERSION — строки старой "
              "версии занулятся и пересчитаются фоном честным as-of")
        return 1
    print("\nсистемных сдвигов не найдено — пересчёт задним числом не нужен")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=60)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.days, a.limit)))
