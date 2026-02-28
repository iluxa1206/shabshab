import os
import sys
import json
import requests
import asyncio
from bs4 import BeautifulSoup
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rates import get_rates_curves
from forwards import CurveBootstrapper, add_months
from valuation import BondRefData, calculate_floater_metrics
from auth import get_access_token, REFRESH_TOKEN
from last_prices import get_last_prices_dict

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

def get_floater_params(isin: str):
    """Fetches base rate, spread and other extra info from floaters.ru"""
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
    search_url = f"https://iss.moex.com/iss/securities.json?q={isin}"
    try:
        resp = requests.get(search_url, timeout=5)
        if resp.status_code != 200: return None
        secid = resp.json()['securities']['data'][0][0]
    except Exception:
        return None

    url = f"https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{secid}.json"
    resp = requests.get(url, timeout=5)
    if resp.status_code != 200:
        return None
    data = resp.json()
    
    try:
        sec_cols = data['securities']['columns']
        sec_data = data['securities']['data'][0]
        params = dict(zip(sec_cols, sec_data))
        return params
    except (KeyError, IndexError):
        return None

def print_cashflow(isin: str, shortname: str, start_date_str: str, end_date_str: str, coupon: str, frequency: str, face_value: float, ruonia_curve, keyrate_curve, calc_date: date):
    if not start_date_str or not end_date_str or not frequency:
        return

    try:
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)
        freq_int = int(frequency)
        if freq_int <= 0:
            return
            
        coupon_parts = coupon.split(' + ')
        coupon_rate = coupon_parts[1].strip() if len(coupon_parts) > 1 else '0%'
        
        base = "RUONIA" if "RUONIA" in coupon else "KEYRATE" if "Ключевая ставка" in coupon else None
        
        delta = end_date - start_date
        total_coupons = freq_int * (delta.days // 360)
        
        coupon_dates = []
        for coup in range(total_coupons):
            coupon_dates.append(start_date + timedelta(days=(360 // freq_int + 1) * (coup + 1)))
            
        try:
            spread_bps = int(float(coupon_rate.replace('%', '').strip()) * 100)
        except ValueError:
            spread_bps = 0
            
        print(f"  Cashflow for {shortname} ({isin}):")
        print(f"    Start Date: {start_date}")
        print(f"    End Date: {end_date}")
        print(f"    Coupon: {coupon}")
        print(f"    Frequency: {frequency} times per year")
        
        future_coupons: list[tuple[int, date, str, float]] = []
        
        prev_date = start_date
        for i, coup_date in enumerate(coupon_dates):
            days = (coup_date - prev_date).days
            alpha = days / 365.0
            
            factor = 0.0
            computed_rate = 0.0
            
            # Если дата купона в прошлом или сегодня, мы не можем оценить будущую ставку по кривой.
            if coup_date <= calc_date:
                pass
            else:
                if base == "RUONIA" and ruonia_curve:
                    start_fwd = max(prev_date, calc_date)
                    if start_fwd < coup_date:
                        F = ruonia_curve.forward(start_fwd, coup_date)
                        computed_rate = F + spread_bps / 10000.0
                        factor = (1.0 + computed_rate / 365.0)**days - 1.0
                elif base == "KEYRATE" and keyrate_curve:
                    start_fwd = max(prev_date, calc_date)
                    if start_fwd < coup_date:
                        F = keyrate_curve.forward(start_fwd, coup_date)
                        computed_rate = F + spread_bps / 10000.0
                        factor = (1.0 + computed_rate / 4.0)**(4.0 * alpha) - 1.0

                payout = face_value * factor
                rate_str = f"{computed_rate * 100:.2f}%" if computed_rate > 0 else "0.00%"
                future_coupons.append((i + 1, coup_date, rate_str, payout))
                
            prev_date = coup_date
            
        print(f"  Total future coupons to be paid: {len(future_coupons)}\n")
        for num, c_date, rate_str, payout in future_coupons:
            print(f"  Coupon {num}: \t {c_date.strftime('%d.%m.%Y')} \t {coupon} \t {rate_str} \t {payout:.2f} \t (Type: COUPON)")
            
        print(f"  Redemption: \t {end_date.strftime('%d.%m.%Y')} \t {' ' * len(coupon)} \t {' ' * 5} \t {face_value:.2f} \t (Type: REDEMPTION)")
    except Exception as e:
        print(f"  [Cashflow] Ошибка при расчете графика: {e}")

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
    
    print("Получение последних цен по WebSocket...")
    access_token = get_access_token(REFRESH_TOKEN)
    last_prices = {}
    if access_token:
        try:
            last_prices = asyncio.run(get_last_prices_dict(access_token, "MOEX", isins))
        except Exception as e:
            print(f"Ошибка при получении цен: {e}")
            
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
                formula = floater_params.get('Купон', 'Неизвестно')
                base_rate = "Неизвестно"
                margin = "0%"
                if formula != "Неизвестно":
                    parts = formula.split("+")
                    base_rate = parts[0].strip()
                    if len(parts) > 1:
                        margin = parts[1].strip()
                
                data = {
                    'SHORTNAME': moex_params.get('SHORTNAME', ''),
                    'MATDATE': moex_params.get('MATDATE'),
                    'COUPONPERCENT': moex_params.get('COUPONPERCENT'),
                    'COUPONPERIOD': moex_params.get('COUPONPERIOD'),
                    'BASE_RATE': base_rate,
                    'MARGIN': margin,
                    'FORMULA': formula,
                    'NEXTCOUPON': moex_params.get('NEXTCOUPON'),
                    'ACCRUEDINT': moex_params.get('ACCRUEDINT'),
                    'FACEVALUE': moex_params.get('FACEVALUE'),
                    'FACEUNIT': moex_params.get('FACEUNIT'),
                    'STARTDATE': floater_params.get('Размещение', ''),
                    'ENDDATE': floater_params.get('Погашение', '') or moex_params.get('MATDATE', ''),
                    'FREQUENCY': floater_params.get('Купонов в год', '')
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
                fv = float(data.get('FACEVALUE', 1000))
            except (ValueError, TypeError):
                fv = 1000.0
                
            print_cashflow(
                isin,
                data.get('SHORTNAME', ''),
                data.get('STARTDATE', ''),
                data.get('ENDDATE', '') or data.get('MATDATE', ''),
                data.get('FORMULA', ''),
                data.get('FREQUENCY', ''),
                fv,
                ruonia_curve,
                irs_curve,
                calc_date
            )
            
            try:
                start_date = date.fromisoformat(data.get('STARTDATE', ''))
                mat_date = date.fromisoformat(data.get('ENDDATE', '') or data.get('MATDATE', ''))
                freq = int(data.get('FREQUENCY', ''))
                coupon_str = data.get('FORMULA', '')
                
                # Parse Base
                if "RUONIA" in coupon_str: base = "RUONIA"
                elif "Ключевая ставка" in coupon_str: base = "KEYRATE"
                else: base = None
                
                # Parse Spread
                coupon_parts = coupon_str.split(' + ')
                coupon_rate = coupon_parts[1].strip() if len(coupon_parts) > 1 else '0%'
                try:
                    spread_bps = int(float(coupon_rate.replace('%', '').strip()) * 100)
                except ValueError:
                    spread_bps = 0
                
                # First Coupon Date
                step_months = 12 // freq
                first_coupon = add_months(start_date, step_months)
                
                # Accrued
                acc_str = data.get('ACCRUEDINT')
                accrued = float(acc_str) if acc_str is not None else 0.0
                
                if base:
                    bond = BondRefData(
                        isin=isin,
                        base=base,
                        spread_issue_bps=spread_bps,
                        face_value=fv,
                        accrued_rub=accrued,
                        maturity_date=mat_date,
                        first_coupon_date=first_coupon,
                        coupons_per_year=freq,
                        issue_date=start_date
                    )
                    
                    curve = ruonia_curve if base == "RUONIA" else irs_curve
                    
                    price = last_prices.get(isin)
                    
                    if price is not None:
                        metrics = calculate_floater_metrics(bond, float(price), curve, calc_date)
                        print(f"  --- Valuation Metrics (Price = {price:.2f}%) ---")
                    else:
                        print(f"  --- Valuation Metrics (Price = 100.00% [DUMMY]) ---")
                        metrics = calculate_floater_metrics(bond, 100.0, curve, calc_date)
                        
                    print(f"  Dirty Price: {metrics['dirty_rub']:,.2f} RUB")
                    if metrics['dm_bps'] is not None:
                        print(f"  DM:          {metrics['dm_bps']} bps")
                        print(f"  Yield (YTM): {metrics['implied_yield_pct']:.4f} %\n")
                    else:
                        print(f"  DM Calculation Failed: Could not bracket root.\n")
            except Exception as e:
                print(f"  [Valuation] Не удалось рассчитать: {e}\n")
                
        else:
            print(f"--- {isin} ---")
            print("Не удалось получить параметры по API\n")

    if cache_updated:
        save_cache(cache, cache_path)

if __name__ == "__main__":
    main()
