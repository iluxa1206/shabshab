import asyncio
import aiohttp
import json
import uuid
import os
from typing import Dict, List, Any
from datetime import date
from auth import get_access_token, REFRESH_TOKEN, BASE_API
from readisins import read_isins_from_file

from rates import get_rates_curves
from forwards import CurveBootstrapper, add_months
from valuation import BondRefData, calculate_floater_metrics

async def get_last_prices_dict(access_token: str, exchange: str, isins: List[str]) -> Dict[str, float]:
    """Возвращает словарь {isin: last_price} из WebSockets"""
    api_base = f"{BASE_API}/md/v2/Securities/{exchange}"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    isin_to_symbol = {}
    symbol_to_isin = {}
    
    async def get_info_for_isin(session: aiohttp.ClientSession, isin: str):
        try:
            async with session.get(f"{api_base}/{isin}") as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    symbol = payload.get("symbol", isin)
                    isin_to_symbol[isin] = symbol
                    symbol_to_isin[symbol] = isin
                else:
                    symbol_to_isin[isin] = isin
        except Exception:
            symbol_to_isin[isin] = isin

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [get_info_for_isin(session, isin) for isin in isins]
        await asyncio.gather(*tasks)

    ws_url = "wss://api.alor.ru/ws"
    symbols_to_fetch = list(symbol_to_isin.keys())
    
    collected_symbols = set()
    result = {}
    timeout = 10.0
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(ws_url) as ws:
                for sym in symbols_to_fetch:
                    sub_msg = {
                        "opcode": "QuotesSubscribe",
                        "exchange": exchange,
                        "code": sym,
                        "format": "Slim",
                        "guid": str(uuid.uuid4()),
                        "token": access_token
                    }
                    await ws.send_str(json.dumps(sub_msg))
                
                start_time = asyncio.get_event_loop().time()
                while len(collected_symbols) < len(symbols_to_fetch):
                    if asyncio.get_event_loop().time() - start_time > timeout:
                        break

                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            payload = data.get("data")
                            if payload and "sym" in payload:
                                sym = payload["sym"]
                                last_price = payload.get("c")
                                
                                if sym in symbol_to_isin and last_price is not None:
                                    isin = symbol_to_isin[sym]
                                    result[isin] = last_price
                                    collected_symbols.add(sym)
                                    
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    except asyncio.TimeoutError:
                        continue
                        
        except Exception as e:
            pass

    return result

def load_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}

async def fetch_last_prices(access_token: str, exchange: str, isins: List[str]):
    api_base = f"{BASE_API}/md/v2/Securities/{exchange}"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(current_dir, "isins_cache.json")
    isins_cache = load_cache(cache_path)

    print("Загрузка кривых (RUONIA, KEYRATE)...")
    ois_quotes, irs_quotes = get_rates_curves(use_cache=True)
    calc_date = date.today()
    if ois_quotes:
        calc_date = ois_quotes[0].date
        
    ruonia_curve = CurveBootstrapper.bootstrap_ruonia(ois_quotes, calc_date)
    irs_curve = CurveBootstrapper.bootstrap_keyrate(irs_quotes, calc_date)

    # 1. Сначала получим тикеры (symbol) и короткие имена для каждого ISIN
    isin_to_symbol = {}
    symbol_to_info = {}
    
    async def get_info_for_isin(session: aiohttp.ClientSession, isin: str):
        try:
            async with session.get(f"{api_base}/{isin}") as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    symbol = payload.get("symbol", isin)
                    shortname = payload.get("shortname", isin)
                    isin_to_symbol[isin] = symbol
                    symbol_to_info[symbol] = {
                        "isin": isin,
                        "shortname": shortname,
                        "last_price": None,
                        "bond_data_cache": isins_cache.get(isin)
                    }
                else:
                    symbol_to_info[isin] = {
                        "isin": isin,
                        "shortname": isins_cache.get(isin, {}).get("SHORTNAME", isin),
                        "last_price": None,
                        "bond_data_cache": isins_cache.get(isin)
                    }
        except Exception:
            symbol_to_info[isin] = {
                "isin": isin, 
                "shortname": isins_cache.get(isin, {}).get("SHORTNAME", isin), 
                "last_price": None,
                "bond_data_cache": isins_cache.get(isin)
            }

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [get_info_for_isin(session, isin) for isin in isins]
        await asyncio.gather(*tasks)

    # 2. Подписка на котировки через WebSockets
    ws_url = "wss://api.alor.ru/ws"
    symbols_to_fetch = list(symbol_to_info.keys())
    
    collected_symbols = set()
    timeout = 10.0 # Ждем максимум 10 секунд на получение всех данных
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(ws_url) as ws:
                for sym in symbols_to_fetch:
                    sub_msg = {
                        "opcode": "QuotesSubscribe",
                        "exchange": exchange,
                        "code": sym,
                        "format": "Slim",
                        "guid": str(uuid.uuid4()),
                        "token": access_token
                    }
                    await ws.send_str(json.dumps(sub_msg))
                
                start_time = asyncio.get_event_loop().time()
                while len(collected_symbols) < len(symbols_to_fetch):
                    if asyncio.get_event_loop().time() - start_time > timeout:
                        print("Tаймаут получения данных от WS.")
                        break

                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            payload = data.get("data")
                            if payload and "sym" in payload:
                                sym = payload["sym"]
                                last_price = payload.get("c")
                                
                                if sym in symbol_to_info and last_price is not None:
                                    symbol_to_info[sym]["last_price"] = last_price
                                    collected_symbols.add(sym)
                                    
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    except asyncio.TimeoutError:
                        continue
                        
        except Exception as e:
            print(f"Ошибка WebSocket: {e}")

    # 3. Вычисление доходности (YTM) и спреда (DM)
    print("Рассчёт метрик доходности и спреда...\n")
    for sym, item in symbol_to_info.items():
        price = item["last_price"]
        item["yield_pct"] = None
        item["dm_bps"] = None

        if price is None or not item["bond_data_cache"]:
            continue
            
        data = item["bond_data_cache"]
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
            
            step_months = 12 // freq
            first_coupon = add_months(start_date, step_months)
            
            acc_str = data.get('ACCRUEDINT')
            accrued = float(acc_str) if acc_str is not None else 0.0
            
            fv = float(data.get('FACEVALUE', 1000.0))
            
            if base:
                bond = BondRefData(
                    isin=item["isin"],
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
                
                metrics = calculate_floater_metrics(bond, float(price), curve, calc_date)
                item["yield_pct"] = metrics.get('implied_yield_pct')
                item["dm_bps"] = metrics.get('dm_bps')
        except Exception as e:
            pass

    # 4. Вывод результатов
    data_list = list(symbol_to_info.values())
    data_list.sort(key=lambda x: x['shortname'])
    
    print(f"{'ISIN':<15} | {'SHORTNAME':<20} | {'LAST PRICE':>12} | {'YIELD (%)':>10} | {'DM (bps)':>10}")
    print("-" * 77)
    for item in data_list:
        price_str = f"{item['last_price']:.2f}" if item['last_price'] is not None else "N/A"
        
        y_val = item.get("yield_pct")
        dm_val = item.get("dm_bps")
        
        y_str = f"{y_val:.2f}%" if y_val is not None and y_val != 0.0 else "N/A"
        dm_str = f"{dm_val}" if dm_val is not None else "N/A"
        
        print(f"{item['isin']:<15} | {item['shortname']:<20} | {price_str:>12} | {y_str:>10} | {dm_str:>10}")
    print()

async def main():
    access_token = get_access_token(REFRESH_TOKEN)
    if not access_token:
        print("Ошибка: не удалось получить токен авторизации.")
        return

    isins = read_isins_from_file()
    if not isins:
        print("Список ISIN пуст.")
        return

    print(f"Получение последних цен для {len(isins)} ISIN...")
    await fetch_last_prices(
        access_token=access_token,
        exchange="MOEX",
        isins=isins
    )

if __name__ == "__main__":
    asyncio.run(main())
