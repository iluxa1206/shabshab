"""Обогащение параметров бумаг из открытых источников: corpbonds.ru (основной,
богатый) + floaters.ru (лёгкий фолбэк, уже в cashflow.get_floater_params).

corpbonds.ru/bond/{ISIN} — server-rendered таблица label→value. Извлекаем:
база/маржа/режим фиксинга из «Формулы купона», погашение, амортизацию, оферты,
номинал. Детект ЭКЗОТИКИ (инверсные MAX(X−КС), капы/флоры, лесенка) — их линейная
модель КС+маржа считать не должна → помечаем base='EXOTIC' (вне универса).

Формула купона:
  «∑КС + 2.3%»            → KEYRATE, average, margin 230
  «КС + 1.5%»            → KEYRATE, point,   margin 150
  «RUONIA + 1.3%»        → RUONIA
  «MAX(КС + 3.75%; 9.5%)»→ KEYRATE + флор 9.5% (маржа 375, floored — считаем прибл.)
  «MAX(25.9% − КС; …)»   → ИНВЕРСНЫЙ → exotic, не моделируется
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; floaters-desk/1.0)"}
_CB_URL = "https://corpbonds.ru/bond/{isin}"


def _parse_formula(f: str) -> dict:
    """Формула купона → {base, margin_bps, mode, exotic, inverse, note}."""
    if not f or f == "—":
        return {}
    raw = f
    up = f.upper().replace("\xa0", " ")
    out: dict = {"formula_text": raw[:120]}

    # режим «среднее»: знак суммы ∑ (U+2211) ИЛИ Σ (U+03A3, греч. сигма — corpbonds
    # шлёт именно её) ИЛИ «СРЕДН». Считаем ДО снятия символа.
    avg = ("∑" in up) or ("Σ" in up) or ("СРЕДН" in up)
    # снять знак суммы/среднего: Σ/∑ — БУКВА, приклеенная к базе («ΣКС») ломает
    # \bКС\b (нет границы слова между буквой Σ и КС) → база не детектилась → бумага
    # ошибочно уходила в EXOTIC. Заменяем на пробел, дальше матчим по очищенному.
    up = re.sub(r"[∑Σ]", " ", up)

    # инверсный: «X% − КС» (КС вычитается из числа) — линейной моделью не считаем
    inverse = bool(re.search(r"[-−–]\s*(КС|KC|CBR|RUONIA)", up))
    # флор/кап/лесенка через MAX/MIN/не более/не менее
    capped = bool(re.search(r"MAX|MIN|МАКС|МИН|НЕ\s+БОЛЕЕ|НЕ\s+МЕНЕЕ|НЕ\s+ВЫШЕ|НЕ\s+НИЖЕ", up))

    base = None
    if "RUONIA" in up:
        base = "RUONIA"
    elif re.search(r"\bКС\b|\bKC\b|КЛЮЧЕВ|CBR", up):
        base = "KEYRATE"

    mode = "average" if avg else "point"

    # маржа: «+ X%» (положительный спред над базой)
    margin_bps = None
    m = re.search(r"\+\s*([0-9]+(?:[.,][0-9]+)?)\s*%", up)
    if m:
        margin_bps = round(float(m.group(1).replace(",", ".")) * 100)

    out.update({"base": base, "margin_bps": margin_bps, "coupon_mode": mode})
    if inverse:
        out["exotic"] = "inverse"          # инверсный — не моделируется линейно
    elif capped:
        out["exotic"] = "capped"           # флор/кап — считаем прибл. как база+маржа
    return out


def _to_iso(dmy: str) -> Optional[str]:
    """'08.04.2027' → '2027-04-08'."""
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", (dmy or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _num(s: str) -> Optional[float]:
    m = re.search(r"([0-9][0-9\s]*(?:[.,][0-9]+)?)", (s or "").replace("\xa0", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_corpbonds_html(html: str) -> dict:
    """HTML страницы corpbonds → нормализованные параметры бумаги."""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "html.parser")
    kv: dict = {}
    for val in s.select("p.val"):
        td = val.find_parent("td")
        row = td.find_parent("tr") if td else None
        if not row:
            continue
        tds = row.find_all("td")
        if len(tds) >= 2:
            label = tds[0].get_text(" ", strip=True)
            # метка до знака '?' (там всплывашка-описание)
            label = label.split("?")[0].strip()
            kv[label] = val.get_text(" ", strip=True)

    out: dict = {"source": "corpbonds"}
    ctype = kv.get("Тип купона", "")
    out["is_floater"] = "Флоатер" in ctype or "флоатер" in ctype
    out.update(_parse_formula(kv.get("Формула купона", "")))
    out["maturity_date"] = _to_iso(kv.get("Дата погашения", ""))
    out["face_value"] = _num(kv.get("Номинал", ""))
    offer = kv.get("Дата ближайшей оферты", "")
    out["has_offer"] = bool(offer) and offer not in ("Нет", "—", "")
    out["has_amort"] = kv.get("Наличие амортизации", "") == "Да"
    out["has_call"] = kv.get("Наличие сall-опциона", kv.get("Наличие call-опциона", "")) == "Да"
    out["is_step"] = kv.get("Купон лесенкой", "") == "Да"
    out["is_subord"] = kv.get("Субординированная облигация", "") == "Да"
    out["rating_raw"] = (kv.get("Кредитный рейтинг") or "").strip() or None
    return out


async def enrich_registry(isins: list, apply: bool = True, delay: float = 0.7) -> dict:
    """Обогащение реестра из corpbonds по списку ISIN (обычно suspect + incomplete).
    Экзотику (инверсные/CPI) → base='EXOTIC'; нормальные флоатеры → base/margin/mode.
    apply=False — сухой прогон (только собрать), без записи. Rate-limit delay сек."""
    import asyncio
    import httpx
    from services import instruments_registry as reg

    stats = {"fetched": 0, "not_found": 0, "exotic": 0, "filled": 0, "confirmed": 0}
    details = []
    async with httpx.AsyncClient(headers=_UA, timeout=15) as client:
        for isin in isins:
            r = await fetch_corpbonds(isin, client=client)
            await asyncio.sleep(delay)
            if r is None:
                stats["not_found"] += 1
                if apply:
                    reg.mark_enrich_attempt(isin, "not_found")
                continue
            stats["fetched"] += 1
            base, margin, exotic = r.get("base"), r.get("margin_bps"), r.get("exotic")
            # экзотика или не-КС/RUONIA флоатер (ИПЦ и т.п.) → вне линейной модели
            if exotic == "inverse" or (r.get("is_floater") and base is None):
                if apply:
                    reg.set_exotic(isin)
                    reg.mark_enrich_attempt(isin, "exotic")
                stats["exotic"] += 1
                details.append((isin, "EXOTIC", r.get("formula_text")))
                continue
            if apply:
                # страница есть, но база/маржа не извлеклись → nodata (перечек по TTL)
                reg.mark_enrich_attempt(
                    isin, "filled" if base in ("KEYRATE", "RUONIA") and margin is not None
                    else "nodata")
            if base in ("KEYRATE", "RUONIA") and margin is not None:
                # coupon_mode НЕ пишем: здешний вывод грубый (бинарное avg → point|
                # average, без лага и без avg_prev), а в реестре он БЬЁТ парсер
                # проспекта (ref_data.coupon_formula читает БД раньше парсера).
                # Парсер валидирован на юниверсе — 0 расхождений режима на 419
                # бумагах, median |err| 0.012пп; оставляем режим за ним.
                fields = {"base": base, "margin_bps": margin,
                          "maturity_date": r.get("maturity_date"),
                          "face_value": r.get("face_value")}
                if apply:
                    reg.apply_authoritative(isin, fields, "corpbonds")
                stats["filled"] += 1
                details.append((isin, f"{base}+{margin}", r.get("formula_text")))
    logger.info("corpbonds enrich: %s", stats)
    return {"stats": stats, "details": details}


async def fetch_corpbonds(isin: str, client=None) -> Optional[dict]:
    """Тянет и парсит corpbonds.ru/bond/{ISIN}. None при недоступности/не-облигации."""
    import httpx
    url = _CB_URL.format(isin=isin)
    try:
        if client is None:
            async with httpx.AsyncClient(headers=_UA, timeout=15) as c:
                resp = await c.get(url)
        else:
            resp = await client.get(url, headers=_UA, timeout=15)
        if resp.status_code != 200 or "Формула купона" not in resp.text:
            return None
        return parse_corpbonds_html(resp.text)
    except Exception as e:
        logger.debug("corpbonds fetch %s: %s", isin, e)
        return None
