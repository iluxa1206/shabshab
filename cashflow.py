import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
import pandas as pd

from rates import get_rates_curves
from forwards import CurveBootstrapper


def read_isins_from_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}


def save_cache(cache: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_local_excel_db(base_dir: str) -> dict[str, dict]:
    pattern_prefix = "bondsearch_"
    pattern_suffix = ".xlsx"
    excel_files = [
        os.path.join(base_dir, name)
        for name in os.listdir(base_dir)
        if name.startswith(pattern_prefix) and name.endswith(pattern_suffix)
    ]
    if not excel_files:
        return {}

    excel_path = max(excel_files, key=os.path.getmtime)
    try:
        df = pd.read_excel(excel_path)
    except Exception:
        return {}

    if "ISIN" not in df.columns:
        return {}

    df = df[df["ISIN"].notna()].copy()
    df["ISIN"] = df["ISIN"].astype(str).str.strip()
    df = df[df["ISIN"] != ""]

    result = {}
    for _, row in df.iterrows():
        isin = row["ISIN"]
        result[isin] = row.to_dict()
    return result


def get_floater_params(isin: str):
    url = f"https://floaters.ru/securities/{isin}"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        params = {}
        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(" ", strip=True)
                if key:
                    params[key] = val
        return params
    except Exception:
        return None


def get_moex_bond_params(isin: str):
    secid = get_moex_secid(isin)
    if not secid:
        return None

    url = f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{secid}.json"
    resp = requests.get(url, timeout=5)
    if resp.status_code != 200:
        return None
    data = resp.json()

    try:
        sec_cols = data["securities"]["columns"]
        sec_data = data["securities"]["data"][0]
        params = dict(zip(sec_cols, sec_data))
        return params
    except (KeyError, IndexError):
        return None


def get_moex_secid(isin: str) -> str | None:
    search_url = f"https://iss.moex.com/iss/securities.json?q={isin}"
    try:
        resp = requests.get(search_url, timeout=5)
        if resp.status_code != 200:
            return None
        return resp.json()["securities"]["data"][0][0]
    except Exception:
        return None


def get_moex_coupon_fallback(isin: str) -> dict | None:
    secid = get_moex_secid(isin)
    if not secid:
        return None

    url = f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{secid}/coupons.json"
    resp = requests.get(url, timeout=5)
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        cols = data["coupons"]["columns"]
        rows = data["coupons"]["data"]
    except (KeyError, TypeError):
        return None

    if not rows:
        return None

    today = date.today()
    parsed = []
    for row in rows:
        rec = dict(zip(cols, row))
        cdate_str = rec.get("COUPONDATE")
        try:
            cdate = date.fromisoformat(cdate_str) if cdate_str else None
        except ValueError:
            cdate = None
        parsed.append((cdate, rec))

    # pick nearest future coupon, else last available
    future = [r for r in parsed if r[0] and r[0] >= today]
    chosen = min(future, key=lambda x: x[0]) if future else parsed[-1]

    cdate, rec = chosen
    return {
        "COUPONRATE": rec.get("COUPONRATE"),
        "COUPONVALUE": rec.get("VALUE"),
        "COUPONDATE": rec.get("COUPONDATE"),
        "STARTDATE": rec.get("STARTDATE"),
        "ENDDATE": rec.get("ENDDATE"),
    }


def adjust_following(d: date) -> date:
    if d.weekday() == 5:
        return d + timedelta(days=2)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def parse_base_and_spread(formula: str, base_rate: str | None) -> tuple[str | None, int]:
    base = None
    if base_rate and base_rate != "Неизвестно":
        base = "RUONIA" if "RUONIA" in base_rate else "KEYRATE" if "Ключевая ставка" in base_rate else None
    if base is None and formula:
        if "RUONIA" in formula:
            base = "RUONIA"
        elif "Ключевая ставка" in formula:
            base = "KEYRATE"

    spread_bps = 0
    if formula and "+" in formula:
        parts = formula.split("+", 1)
        rhs = parts[1].strip()
        try:
            spread_bps = int(float(rhs.replace("%", "").strip()) * 100)
        except ValueError:
            spread_bps = 0
    return base, spread_bps


def build_coupon_schedule(start_date: date, end_date: date, period_days: int) -> list[date]:
    if period_days <= 0:
        return []
    dates = []
    unadj = start_date
    while True:
        unadj = unadj + timedelta(days=period_days)
        if unadj > end_date:
            break
        dates.append(adjust_following(unadj))
    if not dates or dates[-1] != adjust_following(end_date):
        dates.append(adjust_following(end_date))
    # remove duplicates after adjustment
    uniq = []
    seen = set()
    for d in dates:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq


def print_cashflow(
    isin: str,
    shortname: str,
    start_date: date | None,
    end_date: date | None,
    coupon_period_days: int | None,
    frequency: int | None,
    face_value: float,
    formula: str,
    base_rate: str | None,
    ruonia_curve,
    keyrate_curve,
    calc_date: date,
    coupon_percent: float | None,
    next_coupon_date: date | None,
):
    if not end_date or not coupon_period_days:
        print("  [Cashflow] Недостаточно данных для построения графика.")
        return

    if not start_date:
        if next_coupon_date:
            start_date = next_coupon_date - timedelta(days=coupon_period_days)
        else:
            print("  [Cashflow] Нет даты начала размещения. Пробую от NEXTCOUPON.")
            return

    schedule = build_coupon_schedule(start_date, end_date, coupon_period_days)
    base, spread_bps = parse_base_and_spread(formula, base_rate)

    freq_display = frequency
    if not freq_display and coupon_period_days:
        freq_display = max(1, round(365 / coupon_period_days))

    print(f"  Cashflow for {shortname} ({isin}):")
    print(f"    Start Date: {start_date}")
    print(f"    End Date: {end_date}")
    print(f"    Coupon: {formula}")
    if freq_display:
        print(f"    Frequency: {freq_display} times per year")

    future_coupons = []
    prev_date = start_date
    for i, coup_date in enumerate(schedule, start=1):
        days = (coup_date - prev_date).days
        alpha = days / 365.0 if days > 0 else 0.0

        computed_rate = 0.0
        factor = 0.0

        if coup_date > calc_date:
            start_fwd = max(prev_date, calc_date)
            if start_fwd < coup_date:
                if base == "RUONIA" and ruonia_curve:
                    fwd = ruonia_curve.forward(start_fwd, coup_date)
                    computed_rate = fwd + spread_bps / 10000.0
                    # индекс daily-comp, маржа simple (конвенция выпуска)
                    factor = (1.0 + fwd / 365.0) ** days - 1.0 + spread_bps / 10000.0 * alpha
                elif base == "KEYRATE" and keyrate_curve:
                    fwd = keyrate_curve.forward(start_fwd, coup_date)
                    computed_rate = fwd + spread_bps / 10000.0
                    factor = computed_rate * alpha  # (КС+m) simple ACT/365
                elif coupon_percent is not None:
                    computed_rate = coupon_percent / 100.0
                    factor = computed_rate * alpha

            payout = face_value * factor
            rate_str = f"{computed_rate * 100:.2f}%" if computed_rate > 0 else "0.00%"
            future_coupons.append((i, coup_date, rate_str, payout))

        prev_date = coup_date

    print(f"  Total future coupons to be paid: {len(future_coupons)}\n")
    for num, c_date, rate_str, payout in future_coupons:
        print(
            f"  Coupon {num}:      {c_date.strftime('%d.%m.%Y')}"
            f"      {formula:<30}      {rate_str:<8}      {payout:,.2f}   (Type: COUPON)"
        )

    redemption_date = adjust_following(end_date)
    print(
        f"  Redemption:    {redemption_date.strftime('%d.%m.%Y')}"
        f"                                              {face_value:,.2f}         (Type: REDEMPTION)"
    )


def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    isins_path = os.path.join(current_dir, "isins.txt")
    cache_path = os.path.join(current_dir, "isins_cache.json")

    ois_quotes, irs_quotes = get_rates_curves(use_cache=True)
    calc_date = date.today()
    if ois_quotes:
        calc_date = ois_quotes[0].date

    ruonia_curve = CurveBootstrapper.bootstrap_ruonia(ois_quotes, calc_date)
    irs_curve = CurveBootstrapper.bootstrap_keyrate(irs_quotes, calc_date)

    isins = read_isins_from_file(isins_path)
    print(f"Загружено {len(isins)} ISIN из файла {isins_path}:\n")

    cache = load_cache(cache_path)
    cache_updated = False

    for isin in isins:
        cached_indicator = ""
        if isin in cache:
            data = cache[isin]
            cached_indicator = " [ИЗ КЭША]"
        else:
            moex_params = get_moex_bond_params(isin)
            floater_params = get_floater_params(isin) or {}

            if moex_params:
                formula = floater_params.get("Купон", "Неизвестно")
                base_rate = "Неизвестно"
                margin = "0%"
                if formula != "Неизвестно":
                    parts = formula.split("+")
                    base_rate = parts[0].strip()
                    if len(parts) > 1:
                        margin = parts[1].strip()

                data = {
                    "SHORTNAME": moex_params.get("SHORTNAME", ""),
                    "MATDATE": moex_params.get("MATDATE"),
                    "COUPONPERCENT": moex_params.get("COUPONPERCENT"),
                    "COUPONPERIOD": moex_params.get("COUPONPERIOD"),
                    "BASE_RATE": base_rate,
                    "MARGIN": margin,
                    "FORMULA": formula,
                    "NEXTCOUPON": moex_params.get("NEXTCOUPON"),
                    "ACCRUEDINT": moex_params.get("ACCRUEDINT"),
                    "FACEVALUE": moex_params.get("FACEVALUE"),
                    "FACEUNIT": moex_params.get("FACEUNIT"),
                    "STARTDATE": floater_params.get("Размещение", ""),
                    "ENDDATE": floater_params.get("Погашение", "") or moex_params.get("MATDATE", ""),
                    "FREQUENCY": floater_params.get("Купонов в год", ""),
                }
                cache[isin] = data
                cache_updated = True
            else:
                data = None

        if data:
            print(f"--- {isin} ({data.get('SHORTNAME', '')}){cached_indicator} ---")
            print(f"Дата погашения:     {data.get('MATDATE')}")
            print(f"Текущий купон (%):  {data.get('COUPONPERCENT')}%")
            print(f"Период купона:      {data.get('COUPONPERIOD')} дн.")
            print(f"Базовая ставка:     {data.get('BASE_RATE')}")
            print(f"Маржа (Спред):      {data.get('MARGIN')}")
            print(f"Формула купона:     {data.get('FORMULA')}")
            print(f"Сл. выплата купона: {data.get('NEXTCOUPON')}")
            print(f"НКД:                {data.get('ACCRUEDINT')}")
            print(f"Номинал:            {data.get('FACEVALUE')} {data.get('FACEUNIT')}")
            print()

            try:
                fv = float(data.get("FACEVALUE", 1000))
            except (ValueError, TypeError):
                fv = 1000.0

            try:
                start_date = date.fromisoformat(data.get("STARTDATE", ""))
            except (ValueError, TypeError):
                start_date = None

            try:
                end_date = date.fromisoformat(data.get("ENDDATE", "") or data.get("MATDATE", ""))
            except (ValueError, TypeError):
                end_date = None

            try:
                coupon_period_days = int(data.get("COUPONPERIOD", ""))
            except (ValueError, TypeError):
                coupon_period_days = None

            try:
                frequency = int(data.get("FREQUENCY", "")) if data.get("FREQUENCY") else None
            except (ValueError, TypeError):
                frequency = None

            try:
                coupon_percent = float(data.get("COUPONPERCENT")) if data.get("COUPONPERCENT") else None
            except (ValueError, TypeError):
                coupon_percent = None

            try:
                next_coupon_date = date.fromisoformat(data.get("NEXTCOUPON")) if data.get("NEXTCOUPON") else None
            except (ValueError, TypeError):
                next_coupon_date = None

            print_cashflow(
                isin=isin,
                shortname=data.get("SHORTNAME", ""),
                start_date=start_date,
                end_date=end_date,
                coupon_period_days=coupon_period_days,
                frequency=frequency,
                face_value=fv,
                formula=data.get("FORMULA", ""),
                base_rate=data.get("BASE_RATE"),
                ruonia_curve=ruonia_curve,
                keyrate_curve=irs_curve,
                calc_date=calc_date,
                coupon_percent=coupon_percent,
                next_coupon_date=next_coupon_date,
            )
            print()
        else:
            print(f"--- {isin} ---")
            print("Не удалось получить параметры по API\n")

    if cache_updated:
        save_cache(cache, cache_path)


if __name__ == "__main__":
    main()
