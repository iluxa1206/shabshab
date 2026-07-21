import os
import json
import time
import asyncio
import threading
import httpx
from typing import Dict, Optional, Tuple, List
from datetime import date

SECURITIES_CACHE_FILE = "securities_cache.json"
_SNAP_TTL = 120.0  # сек: prev/accrued MOEX кэшируем внутридневно, чтоб не бомбить ISS
# Максимальный возраст live-цены Alor: старше — не отдаём как «текущую» (вне торгов /
# при упавшем Alor кэш отдавал цену любой давности; потребители честно падают на
# prev-close/НРД). 12ч покрывает ночь до утреннего прогрева поллером (~07:00 МСК).
_PRICE_MAX_AGE = 12 * 3600.0

from rates import get_rates_curves, Quote
from forwards import CurveBootstrapper, DiscountCurve
from auth import get_access_token, REFRESH_TOKEN
from last_prices import get_last_prices_dict
from cashflow import load_cache, get_local_excel_db

SCHEDULE_CACHE_FILE = "schedule_cache.json"
SCHEDULE_FULL_CACHE_FILE = "schedule_full_cache.json"

# Ограничитель параллельных коннектов к MOEX ISS. iss.moex.com флаки под нагрузкой
# (ConnectTimeout при burst) — держим низкую конкуренцию.
_MOEX_SEM = asyncio.Semaphore(5)


async def _moex_get(client: httpx.AsyncClient, url: str, *, params=None, timeout: float = 6.0):
    """GET к MOEX под семафором. Fail-fast на таймаут (ретрай таймаута под нагрузкой
    только копит задержку). Ретрай один раз только на 429/5xx. None если не удалось."""
    async with _MOEX_SEM:
        for attempt in range(2):
            try:
                resp = await client.get(url, params=params, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError):
                return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(0.4)
                continue
            return resp
    return None


def _load_schedule_cache() -> dict:
    try:
        with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_schedule_cache(cache: dict) -> None:
    try:
        with open(SCHEDULE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


# Process-level cache
market_cache = {
    "ruonia_curve": None,
    "keyrate_curve": None,
    "last_prices": {},
    "last_prices_ts": {},     # {isin: unix-ts последнего обновления} — возраст цены
    "universe_metrics": {},   # {isin: полные метрики вне watchlist} — наполняет фоновый поллер
    "rates_date": None,
    "calc_date": None
}

# _load_curves_sync зовётся через to_thread: без лока два конкурентных запроса при
# холодном кэше запускали двойной bootstrap (сеть+CPU), а multi-key запись могла
# отдать читателю кривую с чужой датой.
_curves_lock = threading.Lock()

class MarketDataService:
    @classmethod
    async def get_curves(cls) -> Tuple[Optional[DiscountCurve], Optional[DiscountCurve], Optional[date], Optional[date]]:
        """Async-обёртка: bootstrap кривых блокирует (sync requests к Cbonds + CPU),
        уводим в поток, чтобы не вешать event loop."""
        return await asyncio.to_thread(cls._load_curves_sync)

    @classmethod
    def _load_curves_sync(cls) -> Tuple[Optional[DiscountCurve], Optional[DiscountCurve], Optional[date], Optional[date]]:
        with _curves_lock:
            # TTL: кэш кривых валиден только на сегодня (после полуночи — вчерашние ставки)
            if market_cache["calc_date"] != date.today():
                market_cache["ruonia_curve"] = None
                market_cache["keyrate_curve"] = None
            if market_cache["ruonia_curve"] and market_cache["keyrate_curve"]:
                return market_cache["ruonia_curve"], market_cache["keyrate_curve"], market_cache["calc_date"], market_cache["rates_date"]

            try:
                # Load curves from rates.py logic
                ois_quotes, irs_quotes = get_rates_curves(use_cache=True)
                if not ois_quotes or not irs_quotes:
                    return None, None, None, None

                calc_date = date.today()
                rates_date = ois_quotes[0].date if ois_quotes else None

                ruonia_curve = CurveBootstrapper.bootstrap_ruonia(ois_quotes, calc_date)
                irs_curve = CurveBootstrapper.bootstrap_keyrate(irs_quotes, calc_date)

                # одна атомарная запись (под локом) — читатель не увидит кривую с чужой датой
                market_cache.update({
                    "ruonia_curve": ruonia_curve,
                    "keyrate_curve": irs_curve,
                    "calc_date": calc_date,
                    "rates_date": rates_date,
                    "ois_quotes": ois_quotes,   # для кривых ожиданий z-спреда
                    "irs_quotes": irs_quotes,
                })

                return ruonia_curve, irs_curve, calc_date, rates_date

            except Exception as e:
                print(f"Error loading curves: {e}")
                return None, None, None, None

    _gcurve = None
    _gcurve_date: Optional[str] = None

    @classmethod
    async def get_gcurve(cls):
        """КБД ОФЗ (G-curve) МосБиржи — zero-yields по срокам. Кэш память на день.
        Возвращает zspread.GCurve или None."""
        from services.zspread import GCurve
        today = date.today().isoformat()
        if cls._gcurve is not None and cls._gcurve_date == today:
            return cls._gcurve
        try:
            async with httpx.AsyncClient() as client:
                resp = await _moex_get(
                    client, "https://iss.moex.com/iss/engines/stock/zcyc.json",
                    params={"iss.meta": "off", "iss.only": "yearyields"}, timeout=10)
            if resp is None or resp.status_code != 200:
                return cls._stale_gcurve("MOEX zcyc HTTP fail")
            yy = resp.json().get("yearyields", {})
            cols, data = yy.get("columns", []), yy.get("data", [])
            pi, vi = cols.index("period"), cols.index("value")
            pts = [(float(r[pi]), float(r[vi])) for r in data if r[pi] is not None and r[vi] is not None]
            if len(pts) >= 2:
                cls._gcurve = GCurve(pts)
                cls._gcurve_date = today
        except Exception as e:
            return cls._stale_gcurve(f"G-curve fetch error: {e}")
        return cls._gcurve

    @classmethod
    def _stale_gcurve(cls, reason: str):
        """Отдаёт закэшированную КБД при сбое фетча — но ГРОМКО, с возрастом.
        Раньше stale отдавался молча → z/G-спреды сутками на вчерашней КБД."""
        if cls._gcurve is not None and cls._gcurve_date != date.today().isoformat():
            print(f"WARNING: G-curve STALE (от {cls._gcurve_date}) — {reason}")
        else:
            print(f"G-curve unavailable: {reason}")
        return cls._gcurve

    @classmethod
    async def get_zspread_ctx(cls):
        """(exp_ks, exp_ru, g_curve) для расчёта нашего z-спреда над КБД.
        Кривые ожиданий из своп-котировок; КБД — с MOEX. None-safe."""
        from services.zspread import ExpCurve
        await cls.get_curves()  # гарантирует загрузку котировок в market_cache
        ois = market_cache.get("ois_quotes"); irs = market_cache.get("irs_quotes")
        cd = market_cache.get("calc_date") or date.today()
        g = await cls.get_gcurve()
        exp_ks = ExpCurve(cd, irs, "KEYRATE") if irs else None
        exp_ru = ExpCurve(cd, ois, "RUONIA") if ois else None
        return exp_ks, exp_ru, g

    @classmethod
    async def fetch_last_prices(cls, isins: List[str]) -> Dict[str, float]:
        access_token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
        if not access_token:
            return cls.cached_prices()

        try:
            prices = await get_last_prices_dict(access_token, "MOEX", isins)
            now = time.time()
            market_cache["last_prices"].update(prices)
            market_cache["last_prices_ts"].update({i: now for i in prices})
        except Exception as e:
            print(f"Error fetching prices: {e}")
        return cls.cached_prices()

    @classmethod
    def cached_prices(cls, max_age_sec: float = _PRICE_MAX_AGE) -> Dict[str, float]:
        """Цены из WS-кэша не старше max_age_sec (без нового запроса).
        Цена без таймстампа (легаси-запись) считается протухшей."""
        cutoff = time.time() - max_age_sec
        ts = market_cache.get("last_prices_ts", {})
        return {i: p for i, p in market_cache.get("last_prices", {}).items()
                if ts.get(i, 0.0) >= cutoff}

    _shortnames: Dict[str, str] = {}
    _shortnames_date: Optional[str] = None

    @classmethod
    async def fetch_moex_shortnames(cls) -> Dict[str, str]:
        """{isin: SHORTNAME} по всем бондам MOEX одним запросом (короткое имя выпуска).
        Кэш память+диск на день."""
        today = date.today().isoformat()
        if cls._shortnames and cls._shortnames_date == today:
            return cls._shortnames
        try:
            with open("shortnames_cache.json", "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == today:
                cls._shortnames = d.get("map", {})
                cls._shortnames_date = today
                return cls._shortnames
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        out: Dict[str, str] = {}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json",
                    params={"iss.only": "securities", "iss.meta": "off",
                            "securities.columns": "SECID,ISIN,SHORTNAME", "limit": 10000},
                    timeout=20)
            sec = resp.json().get("securities", {})
            cols, rows = sec.get("columns", []), sec.get("data", [])
            ii, si = (cols.index("ISIN") if "ISIN" in cols else -1), (cols.index("SHORTNAME") if "SHORTNAME" in cols else -1)
            for row in rows:
                isin = row[ii] if ii >= 0 else None
                if isin and si >= 0 and row[si]:
                    out[isin] = row[si]
        except Exception as e:
            print(f"MOEX shortnames error: {e}")
            return cls._shortnames
        if out:
            cls._shortnames, cls._shortnames_date = out, today
            try:
                with open("shortnames_cache.json", "w", encoding="utf-8") as f:
                    json.dump({"date": today, "map": out}, f, ensure_ascii=False)
            except OSError:
                pass
        return cls._shortnames

    _full_mem: Dict[str, dict] = {}
    _full_mem_date: Optional[str] = None

    @classmethod
    def _ensure_full_mem(cls) -> None:
        """Загружает дисковый кэш bondization в память один раз в день."""
        today = date.today().isoformat()
        if cls._full_mem_date == today:
            return
        cls._full_mem_date = today
        try:
            with open(SCHEDULE_FULL_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # v2: добавлен блок offers — старый формат без версии сбрасываем (regen)
            cls._full_mem = (raw.get("items", {})
                             if raw.get("date") == today and raw.get("version") == 2 else {})
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            cls._full_mem = {}

    @classmethod
    def _save_full_disk(cls) -> None:
        try:
            with open(SCHEDULE_FULL_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": cls._full_mem_date, "version": 2, "items": cls._full_mem},
                          f, ensure_ascii=False)
        except OSError:
            pass

    @classmethod
    async def fetch_bond_schedule_full(cls, isin: str) -> dict:
        """Полное расписание MOEX по одной бумаге: купоны (с реальными value/valueprc для
        прошлых/зафиксированных) + амортизации (погашение принципала) + оферты
        (offerdate/offertype/price — тот же запрос bondization, бесплатно).
        Кэш память+диск с TTL на день (bondization стабилен внутри дня; критично для
        фонового расчёта метрик всего юниверса — иначе 453 запроса каждый цикл)."""
        cls._ensure_full_mem()
        if isin in cls._full_mem:
            return cls._full_mem[isin]

        out = {"coupons": [], "amorts": [], "offers": []}
        try:
            # через _moex_get (семафор 5) — иначе gather по всему юниверсу на прогреве
            # даёт 453 одновременных коннекта к ISS → таймауты/дропы.
            # ISS отдаёт bondization страницами по 100 (limit>100 игнорируется) —
            # ПАГИНИРУЕМ по start=, иначе у длинных месячных бумаг (12 лет = 144
            # купона) поток обрывается на 100-м купоне: пробел купоны→погашение в
            # годы, SM/z валятся в минус. amorts/offers приходят с первой страницей.
            async with httpx.AsyncClient() as client:
                start, PAGE, guard = 0, 100, 0
                while guard < 40:  # backstop: 40·100 = 4000 купонов хватит любому
                    guard += 1
                    resp = await _moex_get(
                        client,
                        f"https://iss.moex.com/iss/securities/{isin}/bondization.json",
                        params={"iss.only": "coupons,amortizations,offers",
                                "limit": PAGE, "start": start}, timeout=10)
                    if resp is None or resp.status_code != 200:
                        break
                    j = resp.json()
                    cp = j.get("coupons", {})
                    ccols = cp.get("columns", [])
                    cg = lambda row, n: row[ccols.index(n)] if n in ccols else None
                    crows = cp.get("data", [])
                    for row in crows:
                        end = cg(row, "coupondate")
                        if not end:
                            continue
                        out["coupons"].append({
                            "start": cg(row, "startdate"), "end": end,
                            "value": cg(row, "value"), "valueprc": cg(row, "valueprc"),
                            "face": cg(row, "facevalue"),
                        })
                    if start == 0:  # amorts/offers — только с первой страницы
                        am = j.get("amortizations", {})
                        acols = am.get("columns", [])
                        ag = lambda row, n: row[acols.index(n)] if n in acols else None
                        for row in am.get("data", []):
                            d, val = ag(row, "amortdate"), ag(row, "value")
                            if d and val is not None:
                                out["amorts"].append({"date": d, "value": val})
                        off = j.get("offers", {})
                        ocols = off.get("columns", [])
                        og = lambda row, n: row[ocols.index(n)] if n in ocols else None
                        for row in off.get("data", []):
                            d = og(row, "offerdate") or og(row, "offerdateend")
                            if d:
                                out["offers"].append({"date": d, "type": og(row, "offertype"),
                                                      "price": og(row, "price")})
                    if len(crows) < PAGE:  # последняя страница
                        break
                    start += PAGE
        except Exception as e:
            print(f"bondization error {isin}: {e}")
        # кэшируем только успешную выборку (есть купоны) — пустой ответ MOEX не фиксируем
        if out.get("coupons"):
            cls._full_mem[isin] = out
            cls._save_full_disk()
        return out

    _sec_cache: Dict[str, dict] = {}
    _sec_loaded: bool = False

    @classmethod
    def _load_sec_cache(cls) -> None:
        if cls._sec_loaded:
            return
        try:
            with open(SECURITIES_CACHE_FILE, "r", encoding="utf-8") as f:
                cls._sec_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            cls._sec_cache = {}
        cls._sec_loaded = True

    @classmethod
    async def fetch_moex_securities(cls, isins: List[str]) -> Dict[str, dict]:
        """Справочник MOEX для ПРОИЗВОЛЬНОЙ бумаги (не из кэша): имя, номинал, погашение,
        размещение, период купона, НКД, prev, тип купона. Board-less endpoint.
        Кэш память+диск (справочник стабилен) — MOEX дёргаем только для новых ISIN."""
        out: Dict[str, dict] = {}
        if not isins:
            return out
        cls._load_sec_cache()
        today = date.today().isoformat()
        missing = []
        for isin in isins:
            cached = cls._sec_cache.get(isin)
            # записи старого формата (без secid) перефетчиваем разово: у них могла
            # быть выбрана борд-строка с рублёвым НКД для валютной бумаги.
            # md_date: рыночные поля (prev/accrued) валидны только в день фетча —
            # вечный кэш прайсил бумаги ценой многомесячной давности без пометки.
            if cached is not None and "secid" in cached and cached.get("md_date") == today:
                out[isin] = cached
            else:
                missing.append(isin)
        if not missing:
            return out

        async def _resolve_secid(client: httpx.AsyncClient, isin: str):
            """ISIN→SECID через q-поиск (ОФЗ: board-less endpoint ISIN не резолвит)."""
            resp = await _moex_get(
                client, "https://iss.moex.com/iss/securities.json",
                params={"q": isin, "iss.meta": "off", "limit": 10}, timeout=8)
            if resp is None or resp.status_code != 200:
                return None
            sec = resp.json().get("securities", {})
            cols, rows = sec.get("columns", []), sec.get("data", [])
            if "secid" not in cols or "isin" not in cols:
                return None
            si, ii = cols.index("secid"), cols.index("isin")
            for r in rows:
                if (r[ii] or "").upper() == isin:
                    return r[si]
            return None

        async def fetch_one(client: httpx.AsyncClient, isin: str):
            try:
                url = f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{isin}.json"
                resp = await _moex_get(client, url, timeout=8)
                rows, cols, secid = [], [], isin
                if resp is not None and resp.status_code == 200:
                    sec = resp.json().get("securities", {})
                    cols, rows = sec.get("columns", []), sec.get("data", [])
                if not rows:
                    secid = await _resolve_secid(client, isin)
                    if not secid:
                        return
                    resp = await _moex_get(
                        client,
                        f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{secid}.json",
                        timeout=8)
                    if resp is None or resp.status_code != 200:
                        return
                    sec = resp.json().get("securities", {})
                    cols, rows = sec.get("columns", []), sec.get("data", [])
                    if not rows:
                        return
                # среди борд-строк берём ту, где валюта расчётов совпадает с валютой
                # номинала (у замещающих на TQCB НКД в рублях — миксует единицы);
                # рублёвые бумаги: FACEUNIT=SUR ↔ CURRENCYID=SUR — совпадает.
                gi = lambda r, n: r[cols.index(n)] if n in cols else None
                row = rows[0]
                for r in rows:
                    if gi(r, "CURRENCYID") == gi(r, "FACEUNIT"):
                        row = r
                        break
                g = lambda n: row[cols.index(n)] if n in cols else None
                # цена в % от номинала одинакова на всех бордах — если на выбранном
                # (валютном) борде prev пуст, берём с любого другого (TQCB торгуется чаще)
                prev = g("PREVPRICE")
                if prev is None and "PREVPRICE" in cols:
                    pi = cols.index("PREVPRICE")
                    prev = next((r[pi] for r in rows if r[pi] is not None), None)
                out[isin] = {
                    "secid": g("SECID") or secid,
                    "trade_ccy": g("CURRENCYID"),
                    "name": g("SHORTNAME") or g("SECNAME"),
                    "face": g("FACEVALUE"),
                    "face_unit": g("FACEUNIT") or "RUB",
                    "maturity": g("MATDATE"),
                    "issue": g("ISSUEDATE"),
                    "coupon_period": g("COUPONPERIOD"),
                    "accrued": g("ACCRUEDINT"),
                    "prev": prev,
                    "coupon_type": g("COUPONTYPE"),
                    "md_date": date.today().isoformat(),  # свежесть prev/accrued
                }
            except Exception:
                pass

        async with httpx.AsyncClient() as client:
            await asyncio.gather(*(fetch_one(client, i) for i in missing))
        # обновляем кэш только по реально полученным
        new = {i: out[i] for i in missing if i in out}
        if new:
            cls._sec_cache.update(new)
            try:
                with open(SECURITIES_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cls._sec_cache, f, ensure_ascii=False)
            except OSError:
                pass
        return out

    _snap_cache: Dict[str, tuple] = {}

    @classmethod
    async def fetch_moex_snapshot(cls, isins: List[str]) -> Dict[str, dict]:
        """{isin: {'prev': float|None, 'accrued': float|None}} по MOEX ISS.
        Память-кэш TTL 120с — при повторном тогле/открытии карточки не бомбим ISS."""
        out: Dict[str, dict] = {}
        if not isins:
            return out
        now = time.time()
        missing = []
        for isin in isins:
            c = cls._snap_cache.get(isin)
            if c and now - c[1] < _SNAP_TTL:
                out[isin] = c[0]
            else:
                missing.append(isin)
        if not missing:
            return out

        async def fetch_one(client: httpx.AsyncClient, isin: str):
            try:
                url = f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/TQCB/securities/{isin}.json"
                resp = await _moex_get(client, url, timeout=6)
                if resp is None or resp.status_code != 200:
                    return
                j = resp.json()
                sec = j.get("securities", {})
                s_cols, s_rows = sec.get("columns", []), sec.get("data", [])
                prev = accrued = None
                if s_rows:
                    row = s_rows[0]
                    # PREVPRICE и ACCRUEDINT — оба в блоке securities (стабильны при закрытом рынке)
                    if "PREVPRICE" in s_cols:
                        prev = row[s_cols.index("PREVPRICE")]
                    if "ACCRUEDINT" in s_cols:
                        accrued = row[s_cols.index("ACCRUEDINT")]
                    # фолбэк на marketdata.LAST/PREVPRICE если в securities пусто
                if prev is None:
                    md = j.get("marketdata", {})
                    md_cols, md_rows = md.get("columns", []), md.get("data", [])
                    if md_rows and "PREVPRICE" in md_cols:
                        prev = md_rows[0][md_cols.index("PREVPRICE")]
                out[isin] = {
                    "prev": float(prev) if prev is not None else None,
                    "accrued": float(accrued) if accrued is not None else None,
                }
            except Exception:
                pass

        async with httpx.AsyncClient() as client:
            await asyncio.gather(*(fetch_one(client, i) for i in missing))
        for isin in missing:
            if isin in out:
                cls._snap_cache[isin] = (out[isin], now)
        return out

    _board_snap: Dict[str, dict] = {}
    _board_snap_ts: float = 0.0

    @classmethod
    async def fetch_board_snapshot(cls) -> Dict[str, dict]:
        """{isin: {'prev','accrued'}} по ВСЕМУ борду TQCB одним запросом — для
        фонового расчёта метрик юниверса без 453 per-isin вызовов. Кэш TTL 120с."""
        now = time.time()
        if cls._board_snap and now - cls._board_snap_ts < _SNAP_TTL:
            return cls._board_snap
        out: Dict[str, dict] = {}
        try:
            async with httpx.AsyncClient() as client:
                resp = await _moex_get(
                    client,
                    "https://iss.moex.com/iss/engines/stock/markets/bonds/boards/TQCB/securities.json",
                    params={"iss.only": "securities",
                            "securities.columns": "ISIN,PREVPRICE,ACCRUEDINT"},
                    timeout=15)
            if resp is not None and resp.status_code == 200:
                sec = resp.json().get("securities", {})
                cols, rows = sec.get("columns", []), sec.get("data", [])
                ii = cols.index("ISIN") if "ISIN" in cols else -1
                pi = cols.index("PREVPRICE") if "PREVPRICE" in cols else -1
                ai = cols.index("ACCRUEDINT") if "ACCRUEDINT" in cols else -1
                for row in rows:
                    isin = row[ii] if ii >= 0 else None
                    if not isin:
                        continue
                    prev = row[pi] if pi >= 0 else None
                    acc = row[ai] if ai >= 0 else None
                    out[isin] = {
                        "prev": float(prev) if prev is not None else None,
                        "accrued": float(acc) if acc is not None else None,
                    }
        except Exception as e:
            print(f"board snapshot error: {e}")
        if out:
            cls._board_snap = out
            cls._board_snap_ts = now
        return out

    @classmethod
    def universe_metrics(cls) -> Dict[str, dict]:
        """Полные метрики юниверса вне watchlist (наполняет фоновый поллер)."""
        return market_cache.get("universe_metrics", {})

    @classmethod
    async def fetch_prev_close_prices(cls, isins: List[str]) -> Dict[str, float]:
        """Prev close (обёртка над snapshot для обратной совместимости)."""
        snap = await cls.fetch_moex_snapshot(isins)
        return {i: v["prev"] for i, v in snap.items() if v.get("prev") is not None}

    _schedule_mem: Dict[str, list] = {}
    _schedule_mem_date: Optional[str] = None

    @classmethod
    async def fetch_coupon_schedules(cls, isins: List[str]) -> Dict[str, list]:
        """{isin: [(start, end, value),...]} — реальное расписание купонов MOEX
        (bondization) с руб. суммой зафиксированных купонов (value; None если купон
        ещё не определён). Кэш память + диск schedule_cache.json с TTL на день:
        value текущего купона фиксируется со временем, вечный кэш давал бы
        перепрогноз уже известного купона."""
        today = date.today().isoformat()
        if cls._schedule_mem_date != today:
            cls._schedule_mem = {}
            cls._schedule_mem_date = today
        result: Dict[str, list] = {}
        if not isins:
            return result
        raw = _load_schedule_cache()
        # формат v2: {"date": ..., "items": {isin: [[start,end,value],...]}}; старый — discard
        disk = raw.get("items", {}) if raw.get("date") == today else {}
        missing = []
        for isin in isins:
            if isin in cls._schedule_mem:
                result[isin] = cls._schedule_mem[isin]
            elif isin in disk:
                sched = [(date.fromisoformat(a), date.fromisoformat(b), v) for a, b, v in disk[isin]]
                cls._schedule_mem[isin] = sched
                result[isin] = sched
            else:
                missing.append(isin)

        if missing:
            async def fetch_one(client: httpx.AsyncClient, isin: str):
                try:
                    url = f"https://iss.moex.com/iss/securities/{isin}/bondization.json?iss.only=coupons&limit=400"
                    resp = await _moex_get(client, url, timeout=8)
                    if resp is None or resp.status_code != 200:
                        return
                    cp = resp.json().get("coupons", {})
                    cols = cp.get("columns", [])
                    if "startdate" not in cols or "coupondate" not in cols:
                        return
                    si, ei = cols.index("startdate"), cols.index("coupondate")
                    vi = cols.index("value") if "value" in cols else None
                    sched = [(row[si], row[ei], (row[vi] if vi is not None else None))
                             for row in cp.get("data", []) if row[si] and row[ei]]
                    if sched:
                        disk[isin] = sched
                        cls._schedule_mem[isin] = [(date.fromisoformat(a), date.fromisoformat(b), v)
                                                   for a, b, v in sched]
                        result[isin] = cls._schedule_mem[isin]
                except Exception:
                    pass

            async with httpx.AsyncClient() as client:
                await asyncio.gather(*(fetch_one(client, i) for i in missing))
            _save_schedule_cache({"date": today, "items": disk})
        return result
            
    @classmethod
    def get_local_bond_cache(cls, cache_path: str) -> Dict[str, dict]:
        return load_cache(cache_path)

    @classmethod
    def get_excel_db(cls, dir_path: str) -> Dict[str, dict]:
        return get_local_excel_db(dir_path)
