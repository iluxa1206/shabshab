import os
import sys
import requests
import json
import re
from datetime import date
from bs4 import BeautifulSoup
from pprint import pprint

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rates_cache.json")
CBONDS_KEYRATE_IRS_URL = "https://cbonds.ru/indexes/93232/" 
CBONDS_RUONIA_OIS_URL = "https://cbonds.ru/indexes/93218/"

class Quote:
    def __init__(self, name: str, tenor: str, value: float, date_obj: date):
        self.name = name
        self.tenor = tenor
        self.value = value
        self.date = date_obj
        
    def __repr__(self):
        return f"Quote(tenor={self.tenor}, value={self.value}%, date={self.date})"

def fetch(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.text

def tenor_to_days(tenor: str) -> int:
    if not tenor: return 0
    if tenor == "ON": return 1
    if tenor == "TN": return 2
    if tenor == "SN": return 3
    unit = tenor[-1].upper()
    try:
        val = float(tenor[:-1])
    except ValueError:
        return 0
    if unit == 'D': return int(val)
    if unit == 'W': return int(val * 7)
    if unit == 'M': return int(val * 30)
    if unit == 'Y': return int(val * 365)
    return 0

def _normalize_tenor(raw: str) -> str | None:
    if not raw: return None
    raw = raw.strip().upper()
    if raw in {"ON", "TN", "SN"}: return raw
    m = re.match(r"^(\d+(?:\.\d+)?)([DWMY])$", raw)
    if not m: return None
    num, unit = m.groups()
    return f"{num}{unit}"

def _extract_tenor(name: str, regex: re.Pattern) -> str | None:
    match = regex.search(name)
    if match: return _normalize_tenor(match.group(1))
    m = re.search(r"\b(ON|TN|SN)\b", name.upper())
    if m: return m.group(1)
    return None

def _find_table_indices(table) -> dict[str, int] | None:
    header_row = table.find("tr")
    if not header_row: return None
    headers = [c.get_text(strip=True).lower() for c in header_row.find_all(["th", "td"])]
    if not headers: return None
    
    name_keys = ("name", "инструмент", "наименование", "index")
    value_keys = ("value", "знач", "послед", "last", "quote")
    date_keys = ("date", "дата")
    
    indices = {}
    for i, h in enumerate(headers):
        if any(k in h for k in name_keys): indices["name"] = i
        elif any(k in h for k in value_keys): indices["value"] = i
        elif any(k in h for k in date_keys): indices["date"] = i
        
    if {"name", "value", "date"} <= indices.keys():
        return indices
    return None

def parse_cbonds_quotes(html: str, pattern: str) -> list[Quote]:
    """Parse quotes from Cbonds HTML table."""
    soup = BeautifulSoup(html, "html.parser")
    quotes = []
    seen = set()
    regex = re.compile(pattern, re.IGNORECASE)

    tables = soup.find_all("table")
    targeted = [(t, _find_table_indices(t)) for t in tables if _find_table_indices(t)]
    tables_to_scan = targeted if targeted else [(t, None) for t in tables]

    for table, indices in tables_to_scan:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3: continue
            
            if indices:
                name_idx, value_idx, date_idx = indices["name"], indices["value"], indices["date"]
            else:
                name_idx, value_idx, date_idx = 0, 1, 2

            if max(name_idx, value_idx, date_idx) >= len(cells): continue

            name = cells[name_idx]
            tenor = _extract_tenor(name, regex)
            if not tenor: continue

            try:
                val_str = cells[value_idx].replace("%", "").replace(" ", "").replace(",", ".").replace("\xa0", "")
                if val_str in {"", "-"}: continue
                value = float(val_str)
                
                parts = cells[date_idx].replace("\xa0", "").strip().split(".")
                if len(parts) == 3:
                    year = int(parts[2])
                    if year < 100: year += 2000
                    d = date(year, int(parts[1]), int(parts[0]))
                    
                    key = (name, tenor, d.isoformat())
                    if key not in seen:
                        seen.add(key)
                        quotes.append(Quote(name, tenor, value, d))
            except (ValueError, IndexError):
                continue
    return sorted(quotes, key=lambda x: (x.date, tenor_to_days(x.tenor)))

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("cache_date") == date.today().isoformat():
                def dicts_to_quotes(dicts):
                    return [Quote(d["name"], d["tenor"], d["value"], date.fromisoformat(d["date"])) for d in dicts]
                return dicts_to_quotes(data["ois"]), dicts_to_quotes(data["irs"])
    except Exception as e:
        print(f"Failed to load cache: {e}")
    return None

def save_cache(ois, irs):
    try:
        def quotes_to_dicts(quotes):
            return [{"name": q.name, "tenor": q.tenor, "value": q.value, "date": q.date.isoformat()} for q in quotes]
        
        data = {
            "cache_date": date.today().isoformat(),
            "ois": quotes_to_dicts(ois),
            "irs": quotes_to_dicts(irs)
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save cache: {e}")

# --- СПФИ МосБиржи: "RUB Key Rate NY Forward Curve" (Cbonds) ---
# Форвардные значения КС по срокам — реальный вход методики НРД (met_float Прил.3).
# Страница рендерится Vue/JS, но текущее значение есть в сыром HTML как embedded JSON:
# "actual_value.numeric":X рядом с "actual_date".
SPFI_INDEX_IDS = {"3M": 98488, "6M": 98490, "9M": 98492, "1Y": 98494, "2Y": 98496,
                  "3Y": 98498, "4Y": 98500, "5Y": 98546, "6Y": 98502, "7Y": 98504,
                  "8Y": 98506, "9Y": 98508, "10Y": 98510}
SPFI_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spfi_cache.json")
# Снапшот 2026-06-30 — фолбэк при недоступности Cbonds (стареет!)
SPFI_KS_FALLBACK = {"3M": 14.0069, "6M": 13.7284, "9M": 13.0211, "1Y": 13.4592,
                    "2Y": 13.0027, "3Y": 13.6501, "4Y": 13.5447, "5Y": 13.6249,
                    "6Y": 13.5255, "7Y": 13.2692, "8Y": 13.7832, "9Y": 13.8422,
                    "10Y": 13.8800}
SPFI_FALLBACK_DATE = date(2026, 6, 30)

_SPFI_VAL_RE = re.compile(r'"actual_value\.numeric"\s*:\s*"?([\d]+(?:[.,]\d+)?)')
_SPFI_DATE_RE = re.compile(r'"actual_date"\s*:\s*"([^"]+)"')


def _parse_spfi_date(raw: str) -> date | None:
    raw = raw.strip()
    try:
        if "-" in raw:
            return date.fromisoformat(raw[:10])
        parts = raw.split(".")
        if len(parts) == 3:
            year = int(parts[2])
            if year < 100:
                year += 2000
            return date(year, int(parts[1]), int(parts[0]))
    except (ValueError, IndexError):
        pass
    return None


def _spfi_fallback() -> list[Quote]:
    print(f"WARNING: SPFI fallback hardcode as-of {SPFI_FALLBACK_DATE} (Cbonds недоступен)")
    return [Quote(f"SPFI KEYRATE {t} (fallback)", t, v, SPFI_FALLBACK_DATE)
            for t, v in SPFI_KS_FALLBACK.items()]


def get_spfi_curve(use_cache: bool = True) -> list[Quote]:
    """Форвардная кривая КС МБ СПФИ с Cbonds (13 теноров). Кэш на день,
    фолбэк на снапшот 2026-06-30 при недоступности/пейволле."""
    import time as _time
    if use_cache and os.path.exists(SPFI_CACHE_FILE):
        try:
            with open(SPFI_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("cache_date") == date.today().isoformat():
                return [Quote(d["name"], d["tenor"], d["value"], date.fromisoformat(d["date"]))
                        for d in data["quotes"]]
        except Exception as e:
            print(f"Failed to load SPFI cache: {e}")

    quotes: list[Quote] = []
    for tenor, idx in SPFI_INDEX_IDS.items():
        try:
            html = fetch(f"https://cbonds.ru/indexes/{idx}/")
            vm = _SPFI_VAL_RE.search(html)
            if not vm:
                continue
            value = float(vm.group(1).replace(",", "."))
            dm = _SPFI_DATE_RE.search(html)
            d = _parse_spfi_date(dm.group(1)) if dm else None
            quotes.append(Quote(f"SPFI KEYRATE {tenor}", tenor, value, d or date.today()))
        except Exception as e:
            print(f"SPFI {tenor} (id {idx}) fetch error: {e}")
        _time.sleep(0.3)

    tenors = {q.tenor for q in quotes}
    long_ok = any(tenor_to_days(t) >= 5 * 365 for t in tenors)
    if len(quotes) < 6 or "3M" not in tenors or not long_ok:
        return _spfi_fallback()

    if use_cache:
        try:
            with open(SPFI_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"cache_date": date.today().isoformat(),
                           "quotes": [{"name": q.name, "tenor": q.tenor, "value": q.value,
                                       "date": q.date.isoformat()} for q in quotes]},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Failed to save SPFI cache: {e}")
    return quotes


def get_rates_curves(use_cache=True):
    """Fetches OIS RUONIA and IRS KEYRATE quotes from Cbonds, using cache if available."""
    if use_cache:
        cached = load_cache()
        if cached:
            print("Loaded rates from cache.")
            return cached

    print("Fetching OIS RUONIA from Cbonds...")
    ois_html = fetch(CBONDS_RUONIA_OIS_URL)
    ois_quotes = parse_cbonds_quotes(ois_html, r"RUONIA\s+([0-9]+(?:\.[0-9]+)?[DWMY]|ON|TN|SN)")
    
    print("Fetching IRS KEYRATE from Cbonds...")
    irs_html = fetch(CBONDS_KEYRATE_IRS_URL)
    irs_quotes = parse_cbonds_quotes(irs_html, r"IRS RUB vs RUB KEYRATE.*?([0-9]+(?:\.[0-9]+)?[DWMY]|ON|TN|SN)")
    
    if use_cache:
        save_cache(ois_quotes, irs_quotes)
        
    return ois_quotes, irs_quotes

if __name__ == "__main__":
    ois, irs = get_rates_curves()
    print("\n--- OIS RUONIA Quotes ---")
    for q in ois:
        print(f"Tenor: {q.tenor:5} | Rate: {q.value:5.2f}% | Date: {q.date}")
        
    print("\n--- IRS KEYRATE Quotes ---")
    for q in irs:
        print(f"Tenor: {q.tenor:5} | Rate: {q.value:5.2f}% | Date: {q.date}")
