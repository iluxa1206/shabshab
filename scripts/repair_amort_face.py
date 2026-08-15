"""Ремонт средневзвеса у бумаг с амортизацией: пересчёт vwap по номиналу даты
расчётов (Т+1).

Зачем. До фикса services/bars.py::_settle_face_fn номинал брался на дату
заключения сделки, а биржа считает VALUE по номиналу на дату исполнения. В дни
перед ступенькой амортизации vwap = value/volume/face занижался ровно на её шаг
(БалтЛизП10 08–10.08.2026: 87.7 против close 98.6). На проде это 2677 дней у 414
бумаг.

Что делает. Для каждой бумаги тянет FACEVALUE дневной истории ОДНИМ запросом,
считает правильный номинал каждого дня тем же правилом, что и bars.py, и там,
где сохранённый номинал не совпал:
  * пересчитывает цену — vwap_new = vwap_old * face_old / face_new (чистая
    арифметика, свечи заново не качаем);
  * ЗАНУЛЯЕТ спреды этого часа: они считались по неправильной цене, и
    пересчитать их можно только честным as-of движком. Дыры внутри тёплого окна
    досчитает ensure_bars (см. _unpriced_in_window), глубже — при следующем
    прогреве этой бумаги.

Идемпотентно: где номинал уже верный, строка не трогается.

Запуск (в контейнере прода или локально из корня репо):
    python scripts/repair_amort_face.py                 # вся глубина, весь юниверс
    python scripts/repair_amort_face.py --isin RU000A108777
    python scripts/repair_amort_face.py --dry-run
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("repair_face")
log.setLevel(logging.INFO)

# Расхождение номинала, ниже которого не вмешиваемся: FACEVALUE у валютных и
# индексируемых бумаг гуляет копейками день ко дню, и переписывать из-за этого
# цену — только плодить шум.
_EPS = 1e-6


def _suspects(min_dev: float) -> list[str]:
    """Бумаги, у которых средневзвес дня расходится с закрытием больше чем на
    min_dev — кандидаты на кривой номинал."""
    from services.portfolio_db import _connect
    with _connect() as c:
        return [r[0] for r in c.execute(
            "SELECT isin FROM bar_daily WHERE wap_pct IS NOT NULL AND close_pct IS NOT NULL "
            "AND abs(wap_pct/close_pct-1)>? GROUP BY isin", (min_dev,))]


async def repair_one(isin: str, dry: bool) -> tuple[int, int]:
    """(починено часов, затронуто дней) по одной бумаге."""
    import httpx
    from services.bars import fetch_daily_face, _settle_face_fn, _DEFAULT_FACE
    from services.backdate import resolve_market
    from services.portfolio_db import _connect, _lock

    with _connect() as c:
        rows = c.execute(
            "SELECT ts, vwap_pct, face FROM bar_hourly WHERE isin=? ORDER BY ts",
            (isin,)).fetchall()
    if not rows:
        return 0, 0
    frm, till = rows[0]["ts"][:10], rows[-1]["ts"][:10]

    secid, board = await resolve_market(isin, None)
    async with httpx.AsyncClient() as client:
        faces = await fetch_daily_face(client, secid or isin, board or "TQCB", frm, till)
    if not faces:
        log.warning("%s: FACEVALUE не отдался — пропуск", isin)
        return 0, 0
    face_of = _settle_face_fn(faces, _DEFAULT_FACE)

    fix = []
    days = set()
    for r in rows:
        day = r["ts"][:10]
        old, vwap = r["face"], r["vwap_pct"]
        if not old or vwap is None:
            continue
        new = face_of(day)
        if not new or abs(new - old) <= _EPS:
            continue
        fix.append((round(vwap * old / new, 4), new, isin, r["ts"]))
        days.add(day)
    if not fix or dry:
        return len(fix), len(days)

    with _lock, _connect() as c:
        # спред считался по неправильной цене — зануляем; цену чиним арифметикой
        c.executemany(
            "UPDATE bar_hourly SET vwap_pct=?, face=?, y_idx_bps=NULL, dm_bps=NULL, "
            "g_spread_bps=NULL, ytm=NULL, y_open_bps=NULL, y_high_bps=NULL, "
            "y_low_bps=NULL, y_close_bps=NULL, y_idx_alt_bps=NULL, horizon=NULL, "
            "alt_horizon=NULL, metrics_ver=NULL WHERE isin=? AND ts=?", fix)
    return len(fix), len(days)


async def main(a) -> None:
    from services.portfolio_db import init_db
    from services import bars as bars_svc

    init_db()
    isins = [a.isin.upper()] if a.isin else _suspects(a.min_dev)
    log.info("кандидатов: %d", len(isins))
    hours = days = touched = 0
    for n, isin in enumerate(isins, 1):
        try:
            h, d = await repair_one(isin, a.dry_run)
        except Exception as e:
            log.warning("%s: %s", isin, e)
            continue
        if h:
            touched += 1
            hours += h
            days += d
            log.info("%s: %d часов (%d дней)%s", isin, h, d, " [dry]" if a.dry_run else "")
        if n % 25 == 0:
            log.info("%d/%d · бумаг %d · часов %d", n, len(isins), touched, hours)
    log.info("итого: бумаг %d, часов %d, дней %d%s",
             touched, hours, days, " [dry]" if a.dry_run else "")
    if not a.dry_run and hours:
        # дневная свёртка тех же дней: цена изменилась → строка дня пересоберётся
        stat = await bars_svc.build_daily_universe(force=True)
        log.info("свёртка дней: %s", stat)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ремонт vwap при амортизации номинала")
    ap.add_argument("--isin", default=None)
    ap.add_argument("--min-dev", type=float, default=0.005,
                    help="порог расхождения wap/close для отбора кандидатов")
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args()))
