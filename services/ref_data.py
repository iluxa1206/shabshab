"""Справочник параметров облигаций: слой Cbonds-выгрузки + ручные оверрайды.

Разрешение параметров (по убыванию приоритета):
  base, margin, day_count, freq  ← manual > Cbonds-файл > (НРД/MOEX выше по стеку)
  fixing_lag, coupon_mode        ← manual > эмпирический калибратор (coupon_calib)

Cbonds-выгрузка (bondsearch_*.xlsx) даёт базу/маржу/day-count/частоту/тип, но НЕ
лаг фиксинга и не конвенцию point/average — их нет ни у кого, кроме эмиссионных
доков; закрываются калибратором (из истории купонов) или ручным вводом.

Ручной слой (bond_params_manual.json) — редактируемый: новые выпуски + любое
недостающее (лаг, конвенция, маржа для экзотических баз)."""
from __future__ import annotations
import os
import json
import glob
from typing import Dict, Optional

import openpyxl

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANUAL_FILE = os.path.join(_DIR, "bond_params_manual.json")

# База Cbonds -> наш тип
_BASE_MAP = {
    "ключевая ставка цб рф": "KEYRATE",
    "ключевая ставка": "KEYRATE",
    "cbr_rate": "KEYRATE",
    "ruonia": "RUONIA",
    "1/2_cbr_rate": "KEYRATE",  # половинная — обрабатывать отдельно, помечаем базу
}

# Имена колонок в bondsearch-выгрузке (сопоставление по заголовку, не по индексу)
_COL = {
    "isin": ["ISIN"],
    "name": ["Бумага", "Название"],
    "base": ["Базовая ставка", "Базовый индекс (для FRN)"],
    "margin": ["Маржа", "Премия/Дисконт к базовому индексу (для FRN)"],
    "day_count": ["Метод расчета НКД"],
    "freq": ["Периодичность купона", "Купон (раз/год)"],
    "var_type": ["Тип переменной ставки купона"],
    "put": ["Оферта (put)"],
    "coupon_text": ["Купон"],   # текст формулы из проспекта: режим фиксинга + лаг
}

_cbonds_cache: Optional[Dict[str, dict]] = None
_manual_cache: Optional[Dict[str, dict]] = None


def _hdr_index(headers: list) -> Dict[str, int]:
    idx = {}
    for key, names in _COL.items():
        for nm in names:
            if nm in headers:
                idx[key] = headers.index(nm)
                break
    return idx


def _to_float(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", ".").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _latest_cbonds_file() -> Optional[str]:
    """Свежайший bondsearch_DD_MM_YYYY.xlsx по ДАТЕ в имени (не по строке —
    иначе 20_11_2025 «больше» 10_07_2026). Фолбэк — mtime."""
    import re
    files = glob.glob(os.path.join(_DIR, "bondsearch_*.xlsx"))
    if not files:
        return None

    def key(f):
        m = re.search(r"bondsearch_(\d{2})_(\d{2})_(\d{4})", os.path.basename(f))
        if m:
            dd, mm, yyyy = m.groups()
            return (int(yyyy), int(mm), int(dd))
        return (0, 0, 0)

    dated = [f for f in files if key(f) != (0, 0, 0)]
    if dated:
        return max(dated, key=key)
    return max(files, key=os.path.getmtime)


def load_cbonds(path: Optional[str] = None) -> Dict[str, dict]:
    """{isin: {name, base, margin_bps, day_count, freq, var_type}} из bondsearch-xlsx.
    base∈{KEYRATE,RUONIA,None}; base_raw хранит исходное имя (для экзотики)."""
    global _cbonds_cache
    if _cbonds_cache is not None and path is None:
        return _cbonds_cache
    path = path or _latest_cbonds_file()
    out: Dict[str, dict] = {}
    if not path or not os.path.exists(path):
        _cbonds_cache = out
        return out
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    headers = [str(h) if h is not None else "" for h in next(it)]
    ix = _hdr_index(headers)
    if "isin" not in ix:
        _cbonds_cache = out
        return out
    for row in it:
        isin = row[ix["isin"]] if ix["isin"] < len(row) else None
        if not isin:
            continue
        base_raw = str(row[ix["base"]]).strip() if ix.get("base") is not None and row[ix["base"]] else None
        base = _BASE_MAP.get((base_raw or "").lower()) if base_raw else None
        margin = _to_float(row[ix["margin"]]) if ix.get("margin") is not None else None
        out[str(isin).strip()] = {
            "name": row[ix["name"]] if ix.get("name") is not None else None,
            "base": base,
            "base_raw": base_raw,
            "margin_bps": round(margin * 100) if margin is not None else None,
            "day_count": row[ix["day_count"]] if ix.get("day_count") is not None else None,
            "freq": _to_float(row[ix["freq"]]) if ix.get("freq") is not None else None,
            "var_type": row[ix["var_type"]] if ix.get("var_type") is not None else None,
            "coupon_text": (str(row[ix["coupon_text"]]).strip()
                            if ix.get("coupon_text") is not None and row[ix["coupon_text"]] else None),
        }
    if path is None or path == _latest_cbonds_file():
        _cbonds_cache = out
    return out


def load_manual() -> Dict[str, dict]:
    """{isin: {...любые оверрайды: base, margin_bps, fixing_lag, coupon_mode, ...}}."""
    global _manual_cache
    if _manual_cache is not None:
        return _manual_cache
    try:
        with open(_MANUAL_FILE, "r", encoding="utf-8") as f:
            _manual_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _manual_cache = {}
    return _manual_cache


def params(isin: str) -> dict:
    """Слитые параметры выпуска: ручной оверрайд поверх Cbonds-справки."""
    p = dict(load_cbonds().get(isin) or {})
    p.update({k: v for k, v in (load_manual().get(isin) or {}).items() if v is not None})
    return p


def coupon_formula(isin: str, coupons: list = None, margin_pct: float = None,
                   face: float = None, calc_date=None, amorts: list = None,
                   idx=None) -> dict:
    """Полная спека купона для точной проекции:
        {base, margin_bps, fixing_lag, fixing_lag_unit, coupon_mode}
    Разрешение: base/margin — manual > Cbonds; fixing_lag/coupon_mode —
    manual > ПАРСЕР ПРОСПЕКТА (Cbonds «Купон», авторитетно) > эмпирический
    калибратор (coupon_calib, из истории купонов).
    Возвращает None-поля, если источник не дал (потребитель применяет дефолт)."""
    p = params(isin)
    out = {
        "base": p.get("base"),
        "margin_bps": p.get("margin_bps"),
        "fixing_lag": p.get("fixing_lag"),
        "fixing_lag_unit": p.get("fixing_lag_unit"),
        "coupon_mode": p.get("coupon_mode"),
    }
    # 1) текст формулы из проспекта (точный режим + лаг, включая рабочие дни)
    if (out["fixing_lag"] is None or out["coupon_mode"] is None) and p.get("coupon_text"):
        try:
            from services.coupon_calib import parse_prospectus_formula
            ps = parse_prospectus_formula(p["coupon_text"])
            if ps:
                if out["coupon_mode"] is None:
                    out["coupon_mode"] = ps["mode"]
                if out["fixing_lag"] is None:
                    out["fixing_lag"] = ps["lag"]
                    out["fixing_lag_unit"] = ps.get("lag_unit", "cal")
        except Exception:
            pass
    # 2) фолбэк: калибровка из истории купонов
    if (out["fixing_lag"] is None or out["coupon_mode"] is None) and coupons and calc_date:
        try:
            from services.coupon_calib import calibrate
            m = margin_pct if margin_pct is not None else (out["margin_bps"] or 0) / 100.0
            spec = calibrate(isin, coupons, m, face or 1000.0, calc_date,
                             base=out["base"] or "KEYRATE", amorts=amorts, idx=idx)
            if spec:
                if out["fixing_lag"] is None:
                    out["fixing_lag"] = spec["lag"]
                if out["coupon_mode"] is None:
                    out["coupon_mode"] = spec["mode"]
        except Exception:
            pass
    if out["fixing_lag"] is not None and out["fixing_lag_unit"] is None:
        out["fixing_lag_unit"] = "cal"
    return out


# Типы переменной ставки Cbonds, при которых купон ПОСЛЕ оферты неопределён
# (эмитент выставит заново) → оценка к оферте. Для обычного флоатера
# («Плавающая ставка», формула КС/RUONIA+m до погашения) пут — лишь опция
# ликвидности: резать поток нельзя (НРД оценивает к погашению; резка давала
# ложные отрицательные SM у премиальных бумаг с путом).
_RESET_VAR_TYPES = ("определяется решением эмитента", "изменение ставки при условии")


def cut_at_offer(isin: str) -> bool:
    """Резать ли поток флоатера по оферте: только если формула купона после
    оферты неизвестна (пересмотр эмитентом). Неизвестный var_type → не резать."""
    vt = (params(isin).get("var_type") or "").lower()
    if params(isin).get("cut_at_offer") is not None:   # ручной оверрайд
        return bool(params(isin)["cut_at_offer"])
    return any(k in vt for k in _RESET_VAR_TYPES)


def reload():
    """Сброс кэшей (после обновления файлов)."""
    global _cbonds_cache, _manual_cache
    _cbonds_cache = None
    _manual_cache = None
