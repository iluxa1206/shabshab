"""Разовый пересмотр рынка на предмет линкеров RUONIA (индексируемый номинал).

Зачем отдельный проход, если детект встроен в дискавери. Дискавери держит
negative-кэш (discovery_seen): каждый ISIN проверяется РОВНО ОДИН РАЗ, и бумаги,
осмотренные до появления ветки линкеров, помечены «не флоатер» навсегда. Этот
скрипт перепроверяет их адресно.

Как ищет. Кандидат — бумага, у которой биржевой номинал на дату поставки БОЛЬШЕ
номинала на сегодня (номинал растёт день ко дню; у фикса он постоянен, у
амортизируемой падает). Кандидатов по всему рынку единицы, поэтому bondization
дёргается только для них. Дальше решает сверка роста номинала с официальным
индексом RUONIA (services.linker.is_ruonia_linked) — она и отсекает золотые,
серебряные и ИПЦ-линкеры, торгующиеся под тем же видом MOEX.

Запуск:
    python -m scripts.detect_ruonia_linkers          # только показать
    python -m scripts.detect_ruonia_linkers --apply  # записать в реестр
"""
import argparse
import asyncio
import logging
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("linkers")

_URL = "https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json"
_COLS = "BOARDID,SECID,ISIN,SHORTNAME,FACEVALUE,FACEVALUEONSETTLEDATE,FACEUNIT"
# Номинал растёт день ко дню — но копейки округления не в счёт: порог тот же,
# что у флага `linked` во вкладке ФИКСЫ (services/fixed_income).
_GROWTH_EPS = 1.0002


async def _candidates() -> dict:
    """{isin: {name, face}} — бумаги с РАСТУЩИМ номиналом по всему рынку."""
    out, start = {}, 0
    async with httpx.AsyncClient(timeout=60) as cl:
        while start < 20000:
            r = await cl.get(_URL, params={"iss.meta": "off", "iss.only": "securities",
                                           "start": start, "securities.columns": _COLS})
            r.raise_for_status()
            sec = r.json()["securities"]
            cols, rows = sec["columns"], sec["data"]
            if not rows:
                break
            for row in rows:
                d = dict(zip(cols, row))
                isin, f, s = d.get("ISIN"), d.get("FACEVALUE"), d.get("FACEVALUEONSETTLEDATE")
                if not isin or isin in out or not f or not s:
                    continue
                if (d.get("FACEUNIT") or "").upper() not in ("SUR", "RUB", "RUR"):
                    continue          # валютные линкеры вне скоупа
                if s > f * _GROWTH_EPS:
                    out[isin] = {"name": d.get("SHORTNAME"), "face": float(f)}
            start += len(rows)
    return out


async def _coupons(isin: str) -> list:
    """Купоны bondization ПРЯМЫМ запросом, минуя дневной кэш MarketDataService.

    Кэш на диске хранит разбор ПРЕЖНЕЙ версии парсера — без initial_face, на
    котором стоит вся сверка. Разовому проходу по десятку кандидатов дешевле
    сходить в ISS, чем зависеть от того, обновился ли дамп сегодня."""
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(f"https://iss.moex.com/iss/securities/{isin}/bondization.json",
                         params={"iss.meta": "off", "iss.only": "coupons", "limit": 100})
        r.raise_for_status()
        cp = r.json()["coupons"]
        cols = cp["columns"]
        g = lambda row, n: row[cols.index(n)] if n in cols else None
        return [{"start": g(row, "startdate"), "end": g(row, "coupondate"),
                 "value": g(row, "value"), "valueprc": g(row, "valueprc"),
                 "face": g(row, "facevalue"), "initial_face": g(row, "initialfacevalue")}
                for row in cp["data"] if g(row, "coupondate")]


async def main(apply: bool) -> int:
    from services import linker as lnk
    from services import instruments_registry as reg

    cands = await _candidates()
    logger.info("кандидатов с растущим номиналом: %d", len(cands))
    found = []
    for isin, info in sorted(cands.items()):
        try:
            coupons = await _coupons(isin)
        except Exception as e:
            logger.warning("%s: расписание не получено (%s)", isin, e)
            continue
        ok = lnk.is_ruonia_linked(coupons, info["face"])
        rate = lnk._fixed_rate_pct(coupons)
        logger.info("%-14s %-14s номинал %-12s ставка %-6s → %s",
                    isin, info["name"], info["face"], rate,
                    "ЛИНКЕР RUONIA" if ok else "нет")
        if ok:
            found.append((isin, {**info, "coupons": coupons}, rate))

    if not apply:
        print(f"\nнайдено линкеров RUONIA: {len(found)} (запись не выполнялась, нужен --apply)")
        return 0

    from datetime import date
    from core.cashflow import coupon_period_from_coupons
    for isin, info, rate in found:
        coupons = info["coupons"]
        row = {"isin": isin, "short_name": info["name"], "base": "RUONIA",
               "face_index": lnk.RUONIA}
        if rate is not None:
            row["margin_bps"] = int(round(rate * 100))
        # тот же набор расчётных полей, что заводит дискавери: без погашения и
        # купонного периода строка не проходит is_priceable и висит в очереди
        # ревью вместо универса
        ends = sorted(c["end"] for c in coupons if c.get("end"))
        starts = sorted(c["start"] for c in coupons if c.get("start"))
        if ends:
            row["maturity_date"] = ends[-1]
        if starts:
            row["issue_date"] = starts[0]
        cpd = coupon_period_from_coupons(coupons, issue_date=row.get("issue_date"),
                                         today=date.today())
        if cpd and cpd > 0:
            row["coupon_period_days"] = cpd
            row["coupons_per_year"] = max(1, round(365 / cpd))
        verdict = reg.upsert(row, source="moex", mark_new=True)
        # negative-кэш дискавери переворачиваем: иначе следующий проход снова
        # считал бы бумагу проверенным фиксом
        reg.mark_discovery_seen(isin, True)
        logger.info("%s → реестр (%s)", isin, verdict)
    print(f"\nзаписано линкеров RUONIA: {len(found)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать найденное в реестр")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
