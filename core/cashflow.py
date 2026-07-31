import os
import json
from datetime import date, timedelta
import pandas as pd


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
        # нормализуем русскую десятичную запятую и хвостовой %: "1,5%" → 1.50
        rhs = parts[1].strip().replace("%", "").replace(",", ".").strip()
        # берём первое число из хвоста (отсекаем возможный текст после спреда)
        import re as _re
        m = _re.match(r"-?\d+(?:\.\d+)?", rhs)
        try:
            spread_bps = int(float(m.group(0)) * 100) if m else 0
        except (ValueError, AttributeError):
            spread_bps = 0
    return base, spread_bps


def coupon_period_from_coupons(coupons, issue_date=None, today: date | None = None) -> int | None:
    """Длина купонного периода (дней) из ФАКТИЧЕСКОГО графика купонов, а не 365/freq.

    Точнее номинального year/N: реальные периоды у флоатеров гуляют на ±день из-за
    business-day adjustment и календарных месяцев. Приоритет:
      1) интервал между двумя последними ПРОШЕДШИМИ купонами (по end-датам ≤ today)
         — текущая правда выпуска;
      2) бумага ещё не платила → МЕДИАНА интервалов графика;
      3) график из одного купона → размещение → первый купон.
    None, если дат не хватает (вызывающий откатится на round(365/freq)).

    ПОЧЕМУ МЕДИАНА, А НЕ КРАЙНИЕ ПЕРИОДЫ. И хвост, и голова графика сплошь и рядом
    нерегулярные стабы: у ИАДОМ 1P67 размещение→первый купон = 59 дней при месячном
    купоне (регулярный интервал 31), у СИА-1 — 78, у СФОСДМИ 01 — 252 при годовом.
    Хвост не лучше: Атомэнпр17 — 43 дня, МЕТИН2P06 — 50, ИАДОМ 1P63 — 28 (февраль).
    Медиана игнорирует оба стаба и не требует истории выплат.

    `coupons` — список dict с ключами start/end (ISO-строки или date); порядок не важен.
    """
    def _d(x):
        if isinstance(x, str):
            try:
                return date.fromisoformat(x)
            except ValueError:
                return None
        return x if isinstance(x, date) else None

    ends = sorted({d for c in (coupons or []) if (d := _d((c or {}).get("end")))})
    if not ends:
        return None
    past = [e for e in ends if today is None or e <= today]
    if len(past) >= 2:
        return (past[-1] - past[-2]).days
    gaps = sorted((ends[i + 1] - ends[i]).days for i in range(len(ends) - 1))
    if gaps:
        return gaps[len(gaps) // 2]
    iss = _d(issue_date)
    if iss and ends[0] > iss:
        return (ends[0] - iss).days
    return None
