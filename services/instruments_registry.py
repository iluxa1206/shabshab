"""Реестр инструментов — источник универса флоатеров.

Реестр хранит все известные ISIN + ключевые расчётные параметры (база/маржа/
погашение/частота/...) в SQLite (data/instruments.db, Docker-том — переживает
редеплой). Наполняется ежедневным sync из Cbonds-выгрузки + замороженного
seed-дампа универса + MOEX; ручной слой (source='manual', manual_locked=1) sync
не затирает.

Расчётная часть (SM/DM/z) работает по нашим кривым поверх реестра.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from services import text_search

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("INSTRUMENTS_DB", _ROOT / "data" / "instruments.db"))

_lock = threading.Lock()

# Поля, которые sync НЕ трогает у записи с manual_locked=1 (ручной приоритет).
# face_value СЮДА НЕ ВХОДИТ СОЗНАТЕЛЬНО: у амортизируемых бумаг текущий номинал
# меняется по графику, MOEX отдаёт его в листинге живьём, а залоченный ручной
# снапшот застывал на дне импорта xlsx и врал в PV/НКД пропорционально
# пропущенным траншам (27 строк на снимке БД: Саммит 1P3 292.8 против 251.2 у
# MOEX, СНХТ БО-02 714.4 против 571.6, sЛЕГЕНД2P1 1000 против 750).
# Обнулить номинал синк не может — upsert пропускает None.
_MANUAL_FIELDS = ("base", "margin_bps", "maturity_date", "issue_date",
                  "coupon_period_days", "coupons_per_year", "day_count",
                  "fixing_lag", "fixing_lag_unit", "coupon_mode",
                  "short_name", "var_type", "cap_pct", "floor_pct", "coupon_text",
                  "avg_window_days", "compounded", "margin_schedule", "face_index")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments(
  isin              TEXT PRIMARY KEY,
  short_name        TEXT,
  base              TEXT,              -- KEYRATE | RUONIA | FIXED | ...
  margin_bps        INTEGER,
  maturity_date     TEXT,             -- ISO
  issue_date        TEXT,
  coupon_period_days INTEGER,
  coupons_per_year  INTEGER,
  day_count         TEXT,
  face_value        REAL,
  var_type          TEXT,
  fixing_lag        INTEGER,
  fixing_lag_unit   TEXT,
  coupon_mode       TEXT,             -- point | average
  rating            TEXT,
  source            TEXT,             -- cbonds | nrd_frozen | moex | manual
  first_seen        TEXT,
  updated_at        TEXT,
  active            INTEGER NOT NULL DEFAULT 1,
  manual_locked     INTEGER NOT NULL DEFAULT 0,
  reviewed          INTEGER NOT NULL DEFAULT 0,   -- новая бумага, ждёт ревью параметров
  margin_check_pp   REAL,                         -- бэк-аут маржи vs факт КС/RUONIA (pp); |>1.5| = подозрение
  -- Пересмотр ставки НА ОФЕРТЕ, пойманный по факту: сдвиг фактической маржи
  -- между купонами до и после прошедшей оферты. Значит эмитент этот выпуск
  -- пересматривает, и к СЛЕДУЮЩЕЙ оферте поток резать нужно (cut_at_offer),
  -- даже если var_type из Cbonds пуст — а он пуст у 115 из 134 бумаг с офертой.
  -- ФЛАГ НИЧЕГО НЕ МЕНЯЕТ В РАСЧЁТЕ: только поднимает бумагу в ревью.
  offer_reset_bps   REAL,
  offer_reset_date  TEXT,                         -- дата той оферты
  next_offer_date   TEXT,                         -- ближайшая будущая (для отбора в ревью)
  cap_pct           REAL,                         -- потолок ставки купона, % годовых (MIN/«не более»)
  floor_pct         REAL,                         -- пол ставки купона, % годовых (MAX/«не менее»)
  coupon_text       TEXT                          -- текст формулы купона (парсится → база/маржа/режим/кэп/флор)
);
CREATE INDEX IF NOT EXISTS ix_instruments_active ON instruments(active);
CREATE TABLE IF NOT EXISTS discovery_seen(
  isin        TEXT PRIMARY KEY,
  is_floater  INTEGER,           -- 1 = флоатер (добавлен в instruments), 0 = фикс/прочее
  checked_at  TEXT
);
CREATE TABLE IF NOT EXISTS enrich_seen(
  isin          TEXT PRIMARY KEY,
  result        TEXT,            -- not_found | nodata | exotic | filled
  attempted_at  TEXT,
  parser_ver    INTEGER          -- версия парсера corpbonds на момент попытки
);
"""

# ALTER для существующих БД (SQLite не поддерживает IF NOT EXISTS в ADD COLUMN)
_MIGRATIONS = [
    "ALTER TABLE instruments ADD COLUMN margin_check_pp REAL",
    "ALTER TABLE instruments ADD COLUMN offer_reset_bps REAL",
    "ALTER TABLE instruments ADD COLUMN offer_reset_date TEXT",
    "ALTER TABLE instruments ADD COLUMN next_offer_date TEXT",
    "ALTER TABLE instruments ADD COLUMN emitter_id INTEGER",   # MOEX EMITTER_ID (группировка)
    "ALTER TABLE instruments ADD COLUMN emitter_name TEXT",    # имя эмитента (display)
    "ALTER TABLE instruments ADD COLUMN cap_pct REAL",         # потолок ставки купона, %
    "ALTER TABLE instruments ADD COLUMN floor_pct REAL",       # пол ставки купона, %
    "ALTER TABLE instruments ADD COLUMN coupon_text TEXT",     # текст формулы купона
    # ДОЛГОВЕЧНАЯ отметка проверки рейтинга. Дедуп в ratings.refresh стоял на
    # факте «рейтинг записан», из-за чего бумага исчезала из очереди НАВСЕГДА и
    # понижение AAA→A не доезжало никогда. Отметка нужна durable (json-кэш
    # гибнет с рестартом, и без неё todo = весь универс каждый рестарт).
    "ALTER TABLE instruments ADD COLUMN rating_checked_at TEXT",
    "ALTER TABLE enrich_seen ADD COLUMN parser_ver INTEGER",   # версия парсера corpbonds
    # Слой bondresearch.ru (index_floaters): наблюдаемые рынком лаг/метод фиксинга.
    # Отдельные колонки (не fixing_lag/coupon_mode) — провенанс: приоритет спеки
    # manual > bondresearch > парсер проспекта > калибратор (ref_data.coupon_formula),
    # и импорт не превращает строку в manual_locked (никакого freeze-trap).
    "ALTER TABLE instruments ADD COLUMN br_fixing_lag INTEGER",
    "ALTER TABLE instruments ADD COLUMN br_coupon_mode TEXT",      # average | point
    # Окно усреднения базовой ставки, дней: NULL = длина купонного периода
    # (average), 1 = точечный фиксинг (бывший point). Единая параметризация.
    "ALTER TABLE instruments ADD COLUMN avg_window_days INTEGER",
    # окно bondresearch-слоя: «Отсечка» = average·окно 1 (point убран из модели)
    "ALTER TABLE instruments ADD COLUMN br_avg_window_days INTEGER",
    # Бэктест спеки фиксинга по ФАКТУ выплат (дневной синк, шаг 8): средняя
    # |ошибка| пересчёта прошлых купонов нашей спекой. Вердикт OK<0.15пп,
    # WARN<0.5пп, BAD иначе — фильтр «спека расходится» в Справочнике.
    "ALTER TABLE instruments ADD COLUMN spec_err_pp REAL",
    "ALTER TABLE instruments ADD COLUMN spec_verdict TEXT",
    "ALTER TABLE instruments ADD COLUMN spec_checked_at TEXT",
    "ALTER TABLE instruments ADD COLUMN spec_n_coupons INTEGER",
    # Капитализация индекса внутри купонного периода: конвенция
    # «Index_end/Index_start − 1» (ВЭБ.РФ, Роснефть, ОФЗ-ПК нового типа).
    # 1 — купон считается по накопленному индексу RUONIA, а не среднему.
    "ALTER TABLE instruments ADD COLUMN compounded INTEGER",
    # Наличие call-опциона ЭМИТЕНТА (corpbonds «Наличие call-опциона»). Трёхзначное:
    # NULL — не знаем (страница не скрейплена/поля не было), 0 — колла нет, 1 — есть.
    # Нужно, потому что MOEX bondization в offertype колл НЕ различает: на всём
    # универсе только 'Оферта' / 'Оферта (состоялось)' / 'Оферта/Погашение', т.е.
    # даты оферт есть, а чья это опция — из MOEX не узнать.
    "ALTER TABLE instruments ADD COLUMN has_call INTEGER",
    # ДАТЫ колла (JSON-массив ISO) из календаря выплат corpbonds. has_call даёт
    # только факт «опцион есть», а горизонт прайсинга требует даты: без неё
    # _preferred_horizon не с чем сравнивать цену. Колл обычно бермудский —
    # хранится весь список, «ближайшая будущая» считается на дату расчёта.
    "ALTER TABLE instruments ADD COLUMN call_dates TEXT",
    # Сверка типа купона со smart-lab (services/smartlab_audit). Внешний источник
    # называет тип словами и о нашей математике ничего не знает — этим и ценен:
    # ловит и ложный FIXED от классификатора, и флоатер с чужой базой.
    # sl_type: 'fixed' | 'floater' | NULL (сайт молчит — не вердикт, а «не знаем»)
    "ALTER TABLE instruments ADD COLUMN sl_type TEXT",
    "ALTER TABLE instruments ADD COLUMN sl_checked_at TEXT",
    "ALTER TABLE instruments ADD COLUMN sl_mismatch INTEGER",   # 1 — расходится с нашим base
    # РУЧНАЯ лесенка маржи по номерам купонов: «7-20=400; 21-24=550» (bps) или
    # JSON [{"from":7,"to":20,"bps":400}]. Парсер проспекта (coupon_calib.
    # parse_margin_schedule) читает лесенку сам, но сознательно молчит там, где
    # ступень стоит на ДРУГОЙ базе — «MAX(инфляция+4%; ставка рефинансирования
    # +1%)» у Ситиматика: та «маржа» не про КС, и лесенка из неё была бы ложной.
    # Тогда лесенку заводят руками, и она бьёт парсер (ref_data.coupon_formula).
    # Купоны вне диапазонов лесенки считаются НЕ плавающими: прайсинг берёт по
    # ним скаляр margin_bps, бэктест спеки их не судит (bond_audit._backtest).
    "ALTER TABLE instruments ADD COLUMN margin_schedule TEXT",
    # ЛИНКЕР: база ИНДЕКСАЦИИ НОМИНАЛА ('RUONIA' | NULL). Не путать с base: у
    # такой бумаги ставка купона фиксирована, а плавает номинал (ВЭБ.РФ
    # ПБО-002Р-58: 1.85% на номинал, растущий по индексу RUONIA ЦБ). base при
    # этом остаётся RUONIA — кривая, конвенция дисконта и база Y-IDX те же, что
    # у обычного RUONIA-флоатера; отличие только в построении потока
    # (core.valuation, ветка `linker`; детект — services.linker).
    "ALTER TABLE instruments ADD COLUMN face_index TEXT",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


_initialized = False


def _ensure() -> None:
    global _initialized
    if _initialized:
        return
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                c.execute(mig)
            except sqlite3.OperationalError:
                pass  # колонка уже есть
    _initialized = True


# Колонки, которые может нести входная запись (upsert). isin обязателен.
_COLS = ("short_name", "base", "margin_bps", "maturity_date", "issue_date",
         "coupon_period_days", "coupons_per_year", "day_count", "face_value",
         "var_type", "fixing_lag", "fixing_lag_unit", "coupon_mode", "rating",
         "cap_pct", "floor_pct", "coupon_text", "avg_window_days", "compounded",
         "has_call", "margin_schedule", "face_index")


def upsert(row: dict, source: str, mark_new: bool = True,
           keep_source: bool = False) -> str:
    """Вставить/обновить одну бумагу. Возвращает 'new' | 'updated' | 'skipped_locked'.
    У записи с manual_locked=1 обновляются только НЕ-manual поля (rating и т.п.).
    None-значения во входе НЕ затирают уже известные (COALESCE-семантика).
    keep_source=True — служебный рефреш (maturity/name/бэкфилл): не переписывать
    source, он отвечает «кто дал расчётные параметры» (провенанс для разбора
    неверной маржи), а не «кто трогал строку последним»."""
    _ensure()
    isin = (row.get("isin") or "").strip()
    if not isin:
        return "skipped_locked"
    with _lock, _conn() as c:
        cur = c.execute("SELECT * FROM instruments WHERE isin=?", (isin,))
        existing = cur.fetchone()
        now = _now()
        if existing is None:
            fields = {k: row.get(k) for k in _COLS}
            cols = ["isin", "source", "first_seen", "updated_at", "reviewed"] + list(_COLS)
            vals = [isin, source, now, now, 0 if mark_new else 1] + [fields[k] for k in _COLS]
            ph = ",".join("?" * len(cols))
            c.execute(f"INSERT INTO instruments({','.join(cols)}) VALUES({ph})", vals)
            return "new"
        locked = bool(existing["manual_locked"])
        updatable = [k for k in _COLS if not (locked and k in _MANUAL_FIELDS)]
        sets, vals = [], []
        for k in updatable:
            v = row.get(k)
            if v is None:
                continue  # не затираем известное None-ом
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return "skipped_locked"
        sets.append("updated_at=?")
        vals.append(now)
        # source обновляем только если не ручная запись и не служебный рефреш
        if not locked and not keep_source:
            sets.append("source=?")
            vals.append(source)
        vals.append(isin)
        c.execute(f"UPDATE instruments SET {','.join(sets)} WHERE isin=?", vals)
        return "updated"


def _period_days_of(isin: str):
    """coupon_period_days из уже сохранённой строки (для валидации частоты)."""
    row = get(isin)
    return (row or {}).get("coupon_period_days")


def set_manual(isin: str, params: dict, lock: bool = True) -> None:
    """Ручной ввод/правка параметров бумаги (admin). lock=True → sync не затрёт."""
    _ensure()
    isin = (isin or "").strip()
    if not isin:
        raise ValueError("isin обязателен")
    # ВАЛИДАЦИЯ ЗДЕСЬ, а не в модели роута: set_manual — единственная точка, через
    # которую проходит и ручной POST, и xlsx-импорт. coupons_per_year > 12 с пустым
    # coupon_period_days вешает generate_coupon_dates намертво (шаг add_months=0).
    cpy = params.get("coupons_per_year")
    if cpy is not None:
        if not (1 <= int(cpy) <= 366):
            raise ValueError("coupons_per_year вне 1..366")
        if int(cpy) > 12 and not (params.get("coupon_period_days")
                                  or _period_days_of(isin)):
            raise ValueError("coupons_per_year > 12 требует coupon_period_days: "
                             "месячная сетка такую частоту не выражает")
    with _lock, _conn() as c:
        exists = c.execute("SELECT 1 FROM instruments WHERE isin=?", (isin,)).fetchone()
        now = _now()
        fields = {k: params.get(k) for k in _COLS if k in params}
        if exists is None:
            cols = ["isin", "source", "first_seen", "updated_at", "reviewed",
                    "manual_locked"] + list(fields.keys())
            vals = [isin, "manual", now, now, 1, 1 if lock else 0] + list(fields.values())
            ph = ",".join("?" * len(cols))
            c.execute(f"INSERT INTO instruments({','.join(cols)}) VALUES({ph})", vals)
        else:
            sets = [f"{k}=?" for k in fields]
            vals = list(fields.values())
            sets += ["updated_at=?", "reviewed=?", "source=?"]
            vals += [now, 1, "manual"]
            if lock:
                sets.append("manual_locked=?")
                vals.append(1)
            vals.append(isin)
            c.execute(f"UPDATE instruments SET {','.join(sets)} WHERE isin=?", vals)
    # правка должна попасть в расчёт немедленно, не через TTL
    invalidate_params_cache(isin)


# Кэш calc_params_map: юниверс-циклы зовут её на каждую бумагу (700+), а
# _conn() на вызов — это PRAGMA+open. TTL короткий, ручная правка сбрасывает
# кэш немедленно (см. set_manual) — правка справочника видна расчёту сразу.
_calc_params_cache = {"ts": 0.0, "map": None}
_CALC_FIELDS = ("base", "margin_bps", "maturity_date", "issue_date",
                "coupon_period_days", "coupons_per_year", "face_value",
                "face_index")


# Версия данных реестра: инкремент при каждой правке (set_manual/reset/br-импорт).
# Дневные кэши производных (календарь выплат и т.п.) включают её в ключ —
# правка Справочника инвалидирует их немедленно, без ожидания смены даты.
_data_version = 0


def data_version() -> int:
    return _data_version


def invalidate_params_cache(isin: str = None) -> None:
    """Сброс кэша calc_params_map (после ручной правки/импорта справочника).
    isin задан — правка одной бумаги, None — массовая (импорт xlsx)."""
    global _data_version
    _data_version += 1
    _calc_params_cache["map"] = None
    _calc_params_cache["ts"] = 0.0
    # реестровый слой ref_data.params() кэшируется отдельно — сбрасываем и его,
    # иначе спека фиксинга (coupon_mode/lag/cap/floor) живёт стейлой до 30с
    try:
        from services import ref_data
        ref_data.invalidate_registry_cache()
    except Exception:
        pass
    # Событийный движок метрик считает бумагу только на смену ЦЕНЫ и держит
    # кэш уровней (isin, цена)→строка: без этого пинка правка спеки/маржи
    # доезжала до таблицы лишь со следующей сделкой (в неликвиде — никогда).
    try:
        from services import universe_stream
        universe_stream.invalidate_params(isin)
    except Exception:
        pass
    # Тот же пинок скринеру: он держит тёплый контекст пересчёта на бумагу
    # (поток купонов, НКД, кривая) и мемо Y-IDX по ценам — после правки спеки
    # оба относятся к прежним параметрам.
    try:
        from services import screener_core
        screener_core.drop_exact_cache(isin)
    except Exception:
        pass


def calc_params_map() -> Dict[str, dict]:
    """{isin: {расчётные поля}} ВСЕХ активных бумаг одним запросом (только
    не-NULL значения). Реестр — источник истины параметров для BondRefData:
    isins_cache/MOEX лишь заполняют пробелы (см. services.bonds)."""
    import time
    now = time.monotonic()
    if _calc_params_cache["map"] is not None and now - _calc_params_cache["ts"] < 15:
        return _calc_params_cache["map"]
    _ensure()
    out: Dict[str, dict] = {}
    with _conn() as c:
        cols = ",".join(("isin",) + _CALC_FIELDS)
        for r in c.execute(f"SELECT {cols} FROM instruments WHERE active=1"):
            d = {k: r[k] for k in _CALC_FIELDS if r[k] is not None}
            if d:
                out[r["isin"]] = d
    _calc_params_cache["map"] = out
    _calc_params_cache["ts"] = now
    return out


# Поля спеки фиксинга, обнуляемые при сбросе ручной правки: дальше спека
# резолвится живьём (bondresearch > парсер > калибратор). Расчётные поля
# (маржа/даты/номинал) НЕ трогаем — значения остаются, но со снятым lock их
# при следующем проходе освежит sync из источников.
_RESET_SPEC_FIELDS = ("coupon_mode", "fixing_lag", "fixing_lag_unit",
                      "avg_window_days", "compounded", "margin_schedule")


def reset_manual(isin: str) -> Optional[dict]:
    """Сброс ручной правки бумаги: manual_locked=0 + обнуление явных полей спеки
    фиксинга. Возвращает снятые значения (для отображения/отката) или None,
    если бумаги нет."""
    _ensure()
    isin = (isin or "").strip()
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM instruments WHERE isin=?", (isin,)).fetchone()
        if r is None:
            return None
        removed = {k: r[k] for k in _RESET_SPEC_FIELDS if r[k] is not None}
        removed["manual_locked"] = r["manual_locked"]
        sets = ", ".join(f"{k}=NULL" for k in _RESET_SPEC_FIELDS)
        c.execute(f"UPDATE instruments SET {sets}, manual_locked=0, updated_at=? WHERE isin=?",
                  (_now(), isin))
    invalidate_params_cache(isin)
    return removed


def mark_reviewed(isin: str) -> None:
    _ensure()
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET reviewed=1, updated_at=? WHERE isin=?", (_now(), isin))


def get(isin: str) -> Optional[dict]:
    _ensure()
    with _conn() as c:
        r = c.execute("SELECT * FROM instruments WHERE isin=?", ((isin or "").strip(),)).fetchone()
        return dict(r) if r else None


def search(q: str, limit: int = 10) -> list[dict]:
    """Поиск бумаги по подстроке ISIN/имени/эмитента (пикер бумаг, /stop бота).
    → [{isin, name, base, rating}].

    Запрос разбирается services/text_search: не нашлось по набранному —
    пробуем чужую раскладку («Ufpgy» это «Газпн») и латинские двойники
    кириллицы. Варианты идут ПО ОЧЕРЕДИ, побеждает первый непустой: объединять
    выдачи нельзя, иначе обычный запрос разбавлялся бы догадками."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, emitter_name, base, rating FROM instruments "
            "ORDER BY short_name").fetchall()

    # Сравниваем В ПИТОНЕ, а не через SQL LIKE: LIKE в SQLite регистронезависим
    # ТОЛЬКО для ASCII, и «газпн» не находил «Газпн3P13R» — поиск работал лишь
    # когда регистр совпал с базой. Полторы тысячи строк на запрос — дешевле,
    # чем держать в схеме отдельную свёрнутую колонку.
    #
    # Правило отбора — общее с таблицей монитора (text_search.make_matcher):
    # токены по «и», допуск одной лишней буквы, ISIN отдельным haystack. Пикер
    # и монитор обязаны судить одинаково: одно и то же имя, набранное одинаково,
    # не может находиться в таблице и не находиться в форме фильтра.
    def run(term: str) -> list[dict]:
        hit = text_search.ranked(
            term, rows,
            lambda r: (r["short_name"], r["emitter_name"], r["isin"]),
            int(limit))
        return [{"isin": r["isin"], "name": r["short_name"] or r["isin"],
                 "base": r["base"], "rating": r["rating"]} for r in hit]

    return text_search.first_hit(q, run)


def ratings_map(isins) -> Dict[str, str]:
    """{isin: rating} для списка ОДНИМ запросом (батч вместо get() в цикле —
    _conn() открывает соединение + PRAGMA на каждый вызов, в цикле по 700 бумаг
    это сотни мс). Пустой rating не включаем."""
    ids = [(i or "").strip() for i in isins if i]
    if not ids:
        return {}
    _ensure()
    out: Dict[str, str] = {}
    with _conn() as c:
        for k in range(0, len(ids), 900):   # лимит SQLite на число переменных
            chunk = ids[k:k + 900]
            ph = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT isin, rating FROM instruments WHERE isin IN ({ph})", chunk):
                if r["rating"]:
                    out[r["isin"]] = r["rating"]
    return out


def labels_map(isins=None) -> Dict[str, dict]:
    """{isin: {name, emitter, base, rating, maturity, margin_bps, coupons_per_year}}
    — подписи для списков, которые сами считаются вне реестра (лента сделок).
    Маржа и частота нужны ленте, чтобы показать формулу купона тем же
    компонентом, что и СПИСОК («КС + 2,50% (12)»). Без isins — весь реестр
    (сотни строк, один запрос); со списком — только он."""
    _ensure()
    q = ("SELECT isin, short_name, emitter_name, base, rating, maturity_date, "
         "margin_bps, coupons_per_year, coupon_period_days, coupon_text, has_call "
         "FROM instruments")
    args: list = []
    ids = [(i or "").strip() for i in (isins or []) if i]
    if isins is not None:
        if not ids:
            return {}
        if len(ids) <= 900:     # больше — упрёмся в лимит переменных SQLite,
            q += f" WHERE isin IN ({','.join('?' * len(ids))})"   # проще взять всё
            args = ids
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return {r["isin"]: {"name": r["short_name"] or r["isin"], "emitter": r["emitter_name"],
                        "base": r["base"], "rating": r["rating"],
                        "maturity": r["maturity_date"],
                        "margin_bps": r["margin_bps"],
                        # частота: фактический период купона авторитетнее
                        # декларированной частоты — тот же приоритет, что в СПИСКЕ
                        "coupons_per_year": (
                            max(1, min(365, round(365 / r["coupon_period_days"])))
                            if r["coupon_period_days"] else r["coupons_per_year"]),
                        "coupon_text": r["coupon_text"],
                        # call-опцион эмитента: MOEX его не различает вовсе,
                        # источник — corpbonds; нужен ленте для маркера «c»
                        "has_call": r["has_call"]} for r in rows}


_BASE_LABEL = {"KEYRATE": "Ключевая ставка", "RUONIA": "RUONIA"}


def is_priceable(row) -> bool:
    """Бумага прайсуема нашим расчётом ⇔ есть база-флоатер, маржа и дата погашения.
    Без любого из трёх SM/DM/z не посчитать (нет базы→нет проекции, нет маржи→
    нет спреда выпуска, нет maturity→поток не терминируется, perp-guard)."""
    return (row["base"] in ("KEYRATE", "RUONIA")
            and row["margin_bps"] is not None
            and bool(row["maturity_date"]))


def set_emitter(isin: str, emitter_id, emitter_name: str) -> None:
    """Записать эмитента (MOEX EMITTER_ID + имя). Статичен → кэш навсегда."""
    _ensure()
    with _lock, _conn() as c:
        # COALESCE: пометка sentinel'ом (id=0, имя не пришло) не должна стирать
        # имя, уже добытое из corpbonds/ручного слоя
        c.execute("UPDATE instruments SET emitter_id=?, emitter_name=COALESCE(?, emitter_name), "
                  "updated_at=? WHERE isin=?",
                  (emitter_id, emitter_name or None, _now(), isin))


def set_rating(isin: str, rating: str) -> None:
    """Записать рейтинг-бакет (AAA…B/NR) из corpbonds.

    Штампуем rating_checked_at: очередь дозагрузки (services.ratings.refresh)
    отсеивает по НЕЙ, а не по факту наличия рейтинга — иначе бумага выпадала
    из очереди навсегда и понижение рейтинга не доезжало."""
    if not rating:
        return
    _ensure()
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET rating=?, rating_checked_at=?, "
                  "updated_at=? WHERE isin=?", (rating, _now(), _now(), isin))


def rating_checked_map(isins) -> Dict[str, str]:
    """{isin: rating_checked_at} — для TTL-очереди дозагрузки рейтингов."""
    _ensure()
    want = [i for i in (isins or []) if i]
    if not want:
        return {}
    out: Dict[str, str] = {}
    with _conn() as c:
        for i in range(0, len(want), 400):
            part = want[i:i + 400]
            ph = ",".join("?" * len(part))
            for r in c.execute(
                    f"SELECT isin, rating_checked_at FROM instruments "
                    f"WHERE isin IN ({ph})", part):
                if r["rating_checked_at"]:
                    out[r["isin"]] = r["rating_checked_at"]
    return out


# Синтетический эмитент Минфина: MOEX EMITTER_ID у ОФЗ нет, а группа нужна.
# Отрицательный — MOEX нумерует положительными, пересечься не может.
_OFZ_EMITTER_ID = -1

# ОФЗ-ПК СТАРОГО ТИПА (29006–29012): купон = средняя RUONIA за ПРЕДЫДУЩИЙ период
# (avg_prev) + фиксированная премия, а не «среднее за свой период» новых выпусков.
# Премия у них есть в листинге MOEX (130–160бп), у 29013+ её нет (маржа 0).
_OFZ_PK_OLD = ("29006", "29007", "29008", "29009", "29010", "29011", "29012")
# Лаг подобран по факту выплат (8 реализованных купонов на бумагу, 2026-08-13):
# окно=период, лаг 2–3 дня → медиана |ошибки| 0.003–0.021пп при марже, совпавшей
# с листингом (131/140/151/160бп против 130/140/150/160). Берём 3 — единое
# правило на серию, разброс между 2 и 3 внутри допуска вердикта (0.15пп).
_OFZ_PK_OLD_LAG = 3


def _is_ofz_pk_name(short_name: Optional[str]) -> bool:
    """Название говорит «ОФЗ-ПК» (серия 29xxx). Тот же критерий, что в SQL-условии
    normalize_ofz_pk — держим их рядом, чтобы не разъехались."""
    s = (short_name or "").strip().upper()
    return s.startswith("ОФЗ 29") or s.startswith("ОФЗ-ПК") or s.startswith("SU29")


def normalize_ofz_pk() -> int:
    """Нормализация ОФЗ-ПК (серия 29xxx, Минфин): дозаполнить недостающие параметры
    правилом, не трогая уже известные значения и manual_locked-строки.

    Новые ОФЗ-ПК (29013+) в MOEX-листинге идут без маржи → margin NULL →
    is_priceable=False → бумаг вообще не было в универсе/аналитике. Факт: купон =
    среднее RUONIA за период с лагом Т-7, маржа 0. Эмитент (Минфин) и суверенный
    рейтинг AAA у MOEX/corpbonds для ОФЗ отсутствуют → правило, как в fixed-вкладке.

    Серия 29xxx — это ПЕРЕМЕННЫЙ купон по определению, поэтому вердикт base='FIXED'
    на ней всегда наш промах (у 29007–29010 он держался с неизвестного момента и
    выкидывал бумаги из универса — а значит и из живого стрима сделок Alor, лента
    по ним отставала на 15 минут ISS). Такой вердикт снимаем, спеку старой серии
    ставим правилом ниже. Возвращает число затронутых строк (max по апдейтам)."""
    _ensure()
    name_cond = "(short_name LIKE 'ОФЗ 29%' OR short_name LIKE 'ОФЗ-ПК%' OR short_name LIKE 'SU29%')"
    old_cond = "(" + " OR ".join(f"short_name LIKE '%{s}%'" for s in _OFZ_PK_OLD) + ")"
    n = 0
    with _lock, _conn() as c:
        now = _now()
        for sql, args in (
            (f"UPDATE instruments SET base='RUONIA', updated_at=? WHERE active=1 AND {name_cond} "
             "AND manual_locked=0 AND (base IS NULL OR base='' OR base='FIXED')", (now,)),
            # старая серия: avg_prev с коротким лагом ПЕРЕЗАПИСЫВАЕТ правило новых
            # (average/7) — его успел налить этот же нормализатор, пока серия
            # числилась фиксом; фит по факту купонов даёт 0.01пп против 1.2–1.9пп
            (f"UPDATE instruments SET coupon_mode='avg_prev', fixing_lag={_OFZ_PK_OLD_LAG}, "
             f"fixing_lag_unit='days', avg_window_days=NULL, updated_at=? "
             f"WHERE active=1 AND {name_cond} AND {old_cond} AND manual_locked=0", (now,)),
            (f"UPDATE instruments SET margin_bps=0, updated_at=? WHERE active=1 AND {name_cond} "
             "AND manual_locked=0 AND margin_bps IS NULL", (now,)),
            (f"UPDATE instruments SET coupon_mode='average', updated_at=? WHERE active=1 AND {name_cond} "
             "AND manual_locked=0 AND (coupon_mode IS NULL OR coupon_mode='')", (now,)),
            (f"UPDATE instruments SET fixing_lag=7, fixing_lag_unit='days', updated_at=? WHERE active=1 AND {name_cond} "
             "AND manual_locked=0 AND fixing_lag IS NULL", (now,)),
            (f"UPDATE instruments SET rating='AAA', updated_at=? WHERE active=1 AND {name_cond} "
             "AND (rating IS NULL OR rating='')", (now,)),
            (f"UPDATE instruments SET emitter_name='Минфин России', updated_at=? WHERE active=1 AND {name_cond} "
             "AND (emitter_name IS NULL OR emitter_name='')", (now,)),
            # MOEX не отдаёт EMITTER_ID для ОФЗ (description без поля) → своей
            # группы у Минфина нет и канон имени на них не работает. Ставим
            # синтетический отрицательный id: с MOEX-нумерацией (положительной)
            # не столкнётся, а группа получается одна на всю серию.
            (f"UPDATE instruments SET emitter_id={_OFZ_EMITTER_ID}, updated_at=? WHERE active=1 AND {name_cond} "
             "AND (emitter_id IS NULL OR emitter_id=0)", (now,)),
        ):
            n = max(n, c.execute(sql, args).rowcount)
    if n:
        # массовая правка (база/маржа/лаг целой серии) — сбрасываем кэш целиком
        invalidate_params_cache()
    return n


_EMITTER_RETRY_DAYS = 7


def isins_missing_emitter(limit: int = 40) -> list[str]:
    """Активные бумаги без emitter_id — для постепенного бэкфилла в поллере.

    Кроме NULL берём и sentinel'ы (emitter_id=0, «MOEX не отдал EMITTER_ID»),
    но не чаще раза в неделю: у свежих выпусков description на ISS доезжает с
    лагом в недели (ТАЛК002P04 стоял нерезолвимым, а потом MOEX отдал 15252).
    Окно по updated_at не даёт drain-loop крутить одни и те же нерезолвимые
    (ОФЗ) внутри одного прохода — set_emitter обновляет метку времени."""
    _ensure()
    stale = (datetime.now(timezone.utc) - timedelta(days=_EMITTER_RETRY_DAYS)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin FROM instruments WHERE active=1 AND "
            "(emitter_id IS NULL OR (emitter_id=0 AND updated_at < ?)) LIMIT ?",
            (stale, limit)).fetchall()
    return [r["isin"] for r in rows]


def canonical_emitter_names(rows) -> dict:
    """{emitter_id: одно каноническое написание}. Имя эмитента приезжает из
    разных источников (MOEX securities, corpbonds, ручной слой) и пишется
    по-разному — «Газпром капитал» / «Газпром Капитал» / «Газпром капитал ООО»,
    «Банк ВТБ» / «Банк ВТБ ПАО» / «Банк ВТБ (ПАО)». Витрина группирует по имени,
    поэтому один эмитент разваливался на несколько строк фильтра и аналитики.

    Склеиваем по emitter_id (MOEX-идентификатор юрлица — надёжнее любой
    нормализации строк: разные ЮЛ с похожими именами не слипнутся). Побеждает
    САМОЕ ЧАСТОЕ написание в реестре, тай-брейк — короткое, затем алфавит:
    правило детерминированное, имён не выдумываем. Строки без emitter_id
    оставляем как есть — склеивать их можно только по тексту, а это риск
    объединить разные юрлица."""
    from collections import Counter
    freq: dict = {}
    for r in rows:
        eid, name = r["emitter_id"], (r["emitter_name"] or "").strip()
        # 0 — sentinel «MOEX не отдал EMITTER_ID», а не юрлицо: под ним лежат
        # чужие друг другу бумаги, склеивать их нельзя
        if not eid or not name:
            continue
        freq.setdefault(eid, Counter())[name] += 1
    return {eid: min(c.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0]
            for eid, c in freq.items()}


def universe_rows(only_floaters: bool = True, only_priceable: bool = True) -> list[dict]:
    """Список бумаг в форме universe-строки. Расчётные поля (base/margin/maturity/
    rating/emitter) — из реестра инструментов.

    only_priceable=True (по умолчанию) — в основной универс попадают только бумаги
    с полным набором расчётных параметров (B3): непрайсуемые (без базы/маржи/
    погашения) висят в очереди ревью, а не мусорят таблицу нулевыми метриками."""
    _ensure()
    with _conn() as c:
        rows = c.execute("SELECT * FROM instruments WHERE active=1").fetchall()
    # одно написание имени на юрлицо — считаем по ВСЕМ активным строкам, чтобы
    # выбор не зависел от того, каким фильтром сейчас режут универс
    canon = canonical_emitter_names(rows)
    out = []
    for r in rows:
        base = r["base"]
        if only_floaters and base not in ("KEYRATE", "RUONIA"):
            continue
        if only_priceable and not is_priceable(r):
            continue
        out.append({
            "isin": r["isin"],
            "name": r["short_name"] or r["isin"],
            "base_rate_type": base or "UNKNOWN",
            "spread_issue_bps": r["margin_bps"],
            "maturity_date": r["maturity_date"],
            "rating": r["rating"],
            "emitter_id": r["emitter_id"],
            "emitter_name": canon.get(r["emitter_id"]) or r["emitter_name"],
            "coupon_period_days": r["coupon_period_days"],
            "coupons_per_year": r["coupons_per_year"],
            # линкер: база индексации номинала (см. services.linker). Витрине
            # нужна, чтобы «RUONIA + 185» у такой бумаги не читалось как
            # обычный плавающий купон — плавает номинал, ставка фиксирована
            "face_index": r["face_index"],
            # None = не знаем (corpbonds не скрейплен), True/False — знаем
            "has_call": None if r["has_call"] is None else bool(r["has_call"]),
        })
    return out


async def fetch_floater_universe() -> list[dict]:
    """Весь юниверс рублёвых флоатеров (KEYRATE/RUONIA) из реестра инструментов.
    Холодный реестр → разовый bootstrap-sync из локальных источников (замороженный
    seed универса + Cbonds + ручной слой), без сети."""
    rows = universe_rows()
    if rows:
        return rows
    try:
        from services import ref_data
        from services.instruments_sync import load_frozen_seed
        sync_from_sources(load_frozen_seed(), ref_data.load_cbonds(), ref_data.load_manual())
    except Exception as e:
        _log.warning("registry bootstrap sync failed: %s", e)
    return universe_rows()


def sync_active_set(traded_isins: set, min_expected: int = 500) -> dict:
    """Держит active в согласии с набором ТОРГУЕМЫХ на MOEX бумаг:
    не-торгуемые (нет в traded_isins — коммерческие/OTC/делистинг) → active=0;
    вернувшиеся в листинг → active=1. Sanity-guard: если listing куцый
    (< min_expected — сбой сети) НЕ деактивируем массово, чтобы не обнулить универс.
    Возвращает {deactivated, reactivated}."""
    _ensure()
    if len(traded_isins) < min_expected:
        return {"deactivated": 0, "reactivated": 0, "skipped": "listing too small"}
    with _lock, _conn() as c:
        rows = c.execute("SELECT isin, active FROM instruments").fetchall()
        deact = react = 0
        now = _now()
        for r in rows:
            traded = r["isin"] in traded_isins
            if r["active"] and not traded:
                c.execute("UPDATE instruments SET active=0, updated_at=? WHERE isin=?", (now, r["isin"]))
                deact += 1
            elif not r["active"] and traded:
                c.execute("UPDATE instruments SET active=1, updated_at=? WHERE isin=?", (now, r["isin"]))
                react += 1
    return {"deactivated": deact, "reactivated": react}


def retire_matured(today_iso: str) -> int:
    """Деактивирует бумаги с погашением < сегодня (active=0). Возвращает число
    ретайрнутых. Список остаётся чистым — мёртвые бумаги не мусорят универс/ревью."""
    _ensure()
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE instruments SET active=0, updated_at=? "
            "WHERE active=1 AND maturity_date IS NOT NULL AND maturity_date < ?",
            (_now(), today_iso))
        return cur.rowcount


def apply_authoritative(isin: str, fields: dict, source: str) -> bool:
    """Перезапись полей из АВТОРИТЕТНОГО источника (corpbonds — формула купона):
    в отличие от upsert (COALESCE, None не затирает), тут ПЕРЕЗАПИСЫВАЕМ заданные
    поля даже поверх известных (corpbonds точнее Cbonds по базе/марже/режиму).
    manual_locked уважается. Обнуляет margin_check_pp (пересчитается). Returns True если применено."""
    _ensure()
    isin = (isin or "").strip()
    upd = {k: v for k, v in fields.items() if k in _COLS and v is not None}
    if not upd:
        return False
    with _lock, _conn() as c:
        row = c.execute("SELECT manual_locked FROM instruments WHERE isin=?", (isin,)).fetchone()
        if row is None or row["manual_locked"]:
            return False
        sets = [f"{k}=?" for k in upd] + ["source=?", "updated_at=?", "margin_check_pp=NULL", "reviewed=1"]
        vals = list(upd.values()) + [source, _now(), isin]
        c.execute(f"UPDATE instruments SET {','.join(sets)} WHERE isin=?", vals)
    # БЕЗ СБРОСА КЭША правка не доезжает до витрины: memo уровней завязан на
    # (calc_date, curves_ts) и пересчёт заказывается только сменой ЦЕНЫ, поэтому
    # в неликвиде исправленная маржа ждала бы до следующего дня.
    invalidate_params_cache(isin)
    return True


def set_exotic(isin: str, note: str = "") -> None:
    """Экзотическая структура (инверсная/CPI/капитализируемая) — линейной моделью
    КС+маржа не считается корректно → base='EXOTIC' (вне универса), reviewed=1.
    note (текст формулы corpbonds) кладём в coupon_text: ложная экзотика в
    admin-ревью видна глазом по формуле, а не только вердиктом."""
    _ensure()
    with _lock, _conn() as c:
        r = c.execute("SELECT manual_locked FROM instruments WHERE isin=?", (isin,)).fetchone()
        if r is None or r["manual_locked"]:
            return
        c.execute("UPDATE instruments SET base='EXOTIC', reviewed=1, margin_check_pp=NULL, "
                  "coupon_text=COALESCE(NULLIF(?, ''), coupon_text), "
                  "updated_at=? WHERE isin=?", (note or "", _now(), isin))
    # смена базы выкидывает бумагу из универса — витрина обязана узнать сразу
    invalidate_params_cache(isin)


def reclassify_fixed(isin: str) -> None:
    """Бумага оказалась фикс-купонной (0 будущих незафикс. купонов) → base='FIXED':
    уходит из флоатер-универса (universe_rows фильтрует по KEYRATE/RUONIA), не
    прайсится как флоатер. reviewed=0 — на подтверждение админом.

    ОФЗ-ПК исключены: у старой серии купон известен на период вперёд и по всему
    опубликованному хвосту графика равен последнему значению, из-за чего эвристика
    «ставка не менялась» на них ложно срабатывает. Тип бумаги здесь знает не
    эвристика, а название серии — normalize_ofz_pk ставит им RUONIA правилом."""
    _ensure()
    with _lock, _conn() as c:
        row = c.execute("SELECT short_name FROM instruments WHERE isin=?", (isin,)).fetchone()
        if row and _is_ofz_pk_name(row["short_name"]):
            return
        c.execute("UPDATE instruments SET base='FIXED', reviewed=0, updated_at=? "
                  "WHERE isin=? AND manual_locked=0", (_now(), isin))
    # бумага уходит из флоатер-универса — витрина и стрим должны узнать сразу
    invalidate_params_cache(isin)


def set_has_call(isin: str, value: Optional[bool]) -> None:
    """Флаг call-опциона эмитента из corpbonds. value=None — «не знаем», не пишем
    (не затираем известное неизвестностью).

    manual_locked НЕ уважаем сознательно: has_call — факт о бумаге, а не параметр
    прайсинга (как rating, который upsert тоже правит на locked-строках). В проде
    544 строки заморожены импортом xlsx (см. scripts/unfreeze_fixing_spec.py); если
    гейтить флаг по manual_locked, у большинства бумаг он навсегда останется NULL.
    Ручной оверрайд остаётся доступен через set_manual (has_call ∈ _COLS)."""
    if value is None:
        return
    _ensure()
    isin = (isin or "").strip()
    if not isin:
        return
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET has_call=?, updated_at=? WHERE isin=?",
                  (1 if value else 0, _now(), isin))


def set_call_dates(isin: str, dates) -> None:
    """Даты call-опциона из corpbonds (список ISO). None — «не знаем», не пишем;
    [] — знаем, что коллов в календаре нет (гасит устаревшие даты).

    manual_locked не уважаем по той же причине, что и set_has_call: это факт о
    бумаге, а не параметр прайсинга, а в проде сотни строк заморожены импортом."""
    if dates is None:
        return
    _ensure()
    isin = (isin or "").strip()
    if not isin:
        return
    import json as _json
    val = _json.dumps(sorted({d for d in dates if d}), ensure_ascii=False)
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET call_dates=?, updated_at=? WHERE isin=?",
                  (val, _now(), isin))


def call_dates_map() -> Dict[str, list]:
    """{isin: [ISO даты колла]} — только бумаги, у которых даты известны и непусты.
    Их десятки, а не тысячи: грузится одним запросом и кэшируется вызывающим
    (services.market_data подмешивает эти даты в оферты MOEX)."""
    _ensure()
    import json as _json
    out: Dict[str, list] = {}
    with _conn() as c:
        rows = c.execute("SELECT isin, call_dates FROM instruments "
                         "WHERE call_dates IS NOT NULL AND call_dates != '[]'").fetchall()
    for r in rows:
        try:
            ds = _json.loads(r["call_dates"])
        except (ValueError, TypeError):
            continue
        if isinstance(ds, list) and ds:
            out[r["isin"]] = [d for d in ds if isinstance(d, str)]
    return out


def set_margin_check(isin: str, diff_pp: Optional[float]) -> None:
    """Записать расхождение бэк-аута маржи (pp) от факта КС/RUONIA. |>1.5| → suspect."""
    _ensure()
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET margin_check_pp=? WHERE isin=?",
                  (round(diff_pp, 3) if diff_pp is not None else None, isin))


_SUSPECT_PP = 1.5


# Сдвиг маржи, с которого считаем, что эмитент ПЕРЕСМАТРИВАЕТ ставку на оферте.
# Ниже — обычный шум бэк-аута (лаг фиксинга, округление купона в рублях).
OFFER_RESET_BPS = 25.0


def set_offer_reset(isin: str, diff_bps, offer_date, next_offer) -> None:
    """Запомнить пересмотр на прошедшей оферте и дату ближайшей будущей."""
    _ensure()
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET offer_reset_bps=?, offer_reset_date=?, "
                  "next_offer_date=? WHERE isin=?",
                  (round(diff_bps) if diff_bps is not None else None,
                   offer_date, next_offer, isin))


def list_offer_reset() -> list[dict]:
    """Выпуски, где ставка уже менялась на оферте, а ВПЕРЕДИ есть ещё одна, и
    поток к ней не режется. Ровно те, где горизонт оценки под вопросом:
    считаем к погашению по марже, которая на оферте изменится.

    Решение резать принимает человек (Справочник → cut_at_offer): бывает, что
    эмитент менял ставку разово, а формула до погашения известна."""
    _ensure()
    today = date.today().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, offer_reset_bps, "
            "offer_reset_date, next_offer_date, var_type "
            "FROM instruments WHERE active=1 AND offer_reset_bps IS NOT NULL "
            "AND ABS(offer_reset_bps) >= ? AND next_offer_date IS NOT NULL "
            "AND next_offer_date > ? ORDER BY next_offer_date",
            (OFFER_RESET_BPS, today)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        from services.ref_data import cut_at_offer
        if cut_at_offer(d["isin"]):     # уже решено резать — вопрос закрыт
            continue
        out.append(d)
    return out


def list_suspect() -> list[dict]:
    """Прайсуемые флоатеры, где бэк-аут маржи расходится с фактом КС/RUONIA >1.5pp
    (вероятно неверная маржа/база из Cbonds) — на ручную проверку."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, maturity_date, margin_check_pp, source "
            "FROM instruments WHERE active=1 AND margin_check_pp IS NOT NULL "
            "AND ABS(margin_check_pp) > ? ORDER BY ABS(margin_check_pp) DESC",
            (_SUSPECT_PP,)).fetchall()
    return [dict(r) for r in rows]


def list_incomplete() -> list[dict]:
    """Активные флоатеры без полного набора расчётных параметров (не прайсуемы):
    нужен ручной ввод базы/маржи/погашения (B3-очередь)."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, maturity_date, source "
            "FROM instruments WHERE active=1").fetchall()
    return [dict(r) for r in rows
            if not is_priceable(r) and (r["base"] in ("KEYRATE", "RUONIA") or r["base"] is None)]


def isins_incomplete_newest_first() -> list[str]:
    """Непрайсуемые флоатеры, СВЕЖИЕ ВПЕРЁД (по дате размещения, затем по дате
    появления в реестре). Порядок важен: карточка биржи знает формулу купона
    в основном у новых выпусков, а квота запросов на прогон конечна — старый
    бэклог не должен вытеснять сегодняшнее размещение."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, base, margin_bps, maturity_date FROM instruments "
            "WHERE active=1 AND (base IS NULL OR base IN ('KEYRATE','RUONIA')) "
            "ORDER BY COALESCE(issue_date, '') DESC, first_seen DESC").fetchall()
    return [r["isin"] for r in rows if not is_priceable(r)]


def isins_missing_issue_date() -> list[str]:
    """Активные бумаги без даты размещения. Нужна не расчёту, а перепопыткам
    обогащения: без неё свежий выпуск неотличим от старого (см. _FRESH_ISSUE_DAYS).
    Порядок — новые записи реестра первыми: у них шанс на добор выше всего."""
    _ensure()
    with _conn() as c:
        rows = c.execute("SELECT isin FROM instruments WHERE active=1 AND "
                         "issue_date IS NULL ORDER BY first_seen DESC").fetchall()
    return [r["isin"] for r in rows]


def list_no_spec() -> list[dict]:
    """Прайсуемые флоатеры БЕЗ источника спеки фиксинга: нет coupon_text (парсеру
    нечего парсить) и нет ручных coupon_mode/fixing_lag. Прайсинг сидит на
    дефолте point/lag0 — бэктест показал систематику до 1-3пп у таких бумаг.
    Цель для corpbonds-обогащения (он принесёт formula_text → coupon_text)."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, maturity_date FROM instruments "
            "WHERE active=1 AND base IN ('KEYRATE','RUONIA') "
            "AND coupon_text IS NULL AND coupon_mode IS NULL AND fixing_lag IS NULL"
        ).fetchall()
    return [dict(r) for r in rows if is_priceable(r)]


def list_call_unknown() -> list[dict]:
    """Активные флоатеры, у которых НЕ известен статус call-опциона (has_call IS NULL).
    Цель corpbonds-обогащения ради маркера p/c у даты погашения: сам по себе
    здоровый флоатер в очередь enrich не попадает (там только incomplete/suspect/
    exotic/no_spec), поэтому без этого класса флаг остался бы пустым у всех.
    Вызывающий сужает список до бумаг с БУДУЩЕЙ офертой (даты знает только
    day-кэш bondization) — скрейпить весь универс ради флага смысла нет."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name FROM instruments "
            "WHERE active=1 AND base IN ('KEYRATE','RUONIA') AND has_call IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def list_call_dates_missing() -> list[dict]:
    """Активные флоатеры, у которых ДАТ колла нет (call_dates IS NULL), а сам колл
    возможен: has_call=1 («есть») или NULL («не знаем»). has_call=0 пропускаем —
    corpbonds уже ответил, что опциона нет, ходить за календарём незачем.
    Цель backfill_call_dates: без даты колл не может стать горизонтом прайсинга."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, has_call, maturity_date FROM instruments "
            "WHERE active=1 AND base IN ('KEYRATE','RUONIA') "
            "AND call_dates IS NULL AND (has_call IS NULL OR has_call=1)"
        ).fetchall()
    return [dict(r) for r in rows]


def list_exotic() -> list[dict]:
    """Активные бумаги, помеченные base='EXOTIC' (не manual_locked). Для периодической
    ПЕРЕПРОВЕРКИ corpbonds: детект экзотики раньше ошибался (напр. Σ-приклеенная база
    «ΣКС» уходила в EXOTIC) — перепарс возвращает ложные экзотики в универс."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, source, coupon_text FROM instruments "
            "WHERE active=1 AND base='EXOTIC' AND manual_locked=0").fetchall()
    return [dict(r) for r in rows]


# Полный набор колонок бумаги для справочника (все расчётные + служебные).
_CATALOG_COLS = ("isin", "short_name", "base", "margin_bps", "maturity_date",
                 "issue_date", "coupon_period_days", "coupons_per_year", "day_count",
                 "face_value", "var_type", "fixing_lag", "fixing_lag_unit",
                 "coupon_mode", "avg_window_days", "compounded",
                 "br_fixing_lag", "br_coupon_mode",
                 "cap_pct", "floor_pct", "coupon_text", "margin_schedule",
                 "rating", "has_call",
                 "source", "reviewed", "manual_locked", "margin_check_pp",
                 "emitter_name", "active",
                 "spec_err_pp", "spec_verdict", "spec_n_coupons", "spec_checked_at",
                 "sl_type", "sl_checked_at", "sl_mismatch")


# Поля купона, которыми ручной слой реестра ПЕРЕОПРЕДЕЛЯЕТ прайсинг (мост в
# ref_data.coupon_formula). Только эти + только manual_locked=1 (явная правка).
_COUPON_OVERRIDE_COLS = ("base", "margin_bps", "fixing_lag", "fixing_lag_unit",
                         "coupon_mode", "cap_pct", "floor_pct", "coupon_text",
                         "avg_window_days", "compounded", "margin_schedule")


def set_br_spec(isin: str, fixing_lag, coupon_mode) -> None:
    """Записать спеку фиксинга слоя bondresearch.ru (br_* колонки). Не трогает
    manual_locked и основные поля — провенанс отдельный, freeze-trap исключён."""
    set_br_specs_bulk({(isin or "").strip(): {"fixing_lag": fixing_lag,
                                              "coupon_mode": coupon_mode}})


def set_br_specs_bulk(specs: Dict[str, dict]) -> int:
    """Батч-запись br-спек {isin: {fixing_lag, coupon_mode, avg_window_days}}
    одним соединением (дневной синк пишет ~450 строк). Возвращает число строк."""
    if not specs:
        return 0
    _ensure()
    now = _now()
    n = 0
    with _lock, _conn() as c:
        for isin, s in specs.items():
            cur = c.execute(
                "UPDATE instruments SET br_fixing_lag=?, br_coupon_mode=?, "
                "br_avg_window_days=?, updated_at=? WHERE isin=?",
                (s.get("fixing_lag"), s.get("coupon_mode"),
                 s.get("avg_window_days"), now, isin))
            n += cur.rowcount
    invalidate_params_cache()
    return n


def set_spec_backtest(isin: str, err_pp, verdict: str, n_coupons: int) -> None:
    """Записать результат бэктеста спеки (дневной синк). verdict OK|WARN|BAD|NO_DATA."""
    _ensure()
    with _lock, _conn() as c:
        c.execute("UPDATE instruments SET spec_err_pp=?, spec_verdict=?, "
                  "spec_n_coupons=?, spec_checked_at=? WHERE isin=?",
                  (err_pp, verdict, n_coupons, _now(), (isin or "").strip()))


def list_spec_mismatch(min_verdict: str = "WARN") -> list[dict]:
    """Бумаги, у которых спека фиксинга (лаг/окно/режим) расходится с фактом
    выплат: вердикт бэктеста WARN/BAD. Для фильтра «спека расходится»."""
    _ensure()
    verdicts = ("WARN", "BAD") if min_verdict == "WARN" else ("BAD",)
    ph = ",".join("?" * len(verdicts))
    with _conn() as c:
        rows = c.execute(
            f"SELECT isin, short_name, base, margin_bps, coupon_mode, fixing_lag, "
            f"fixing_lag_unit, avg_window_days, br_coupon_mode, br_fixing_lag, "
            f"br_avg_window_days, spec_err_pp, spec_verdict, spec_n_coupons, "
            f"spec_checked_at, manual_locked "
            f"FROM instruments WHERE active=1 AND spec_verdict IN ({ph}) "
            f"ORDER BY spec_err_pp DESC", verdicts).fetchall()
    return [dict(r) for r in rows]


_FLOAT_BASES_REG = ("KEYRATE", "RUONIA", "EXOTIC")


def set_smartlab_type(isin: str, sl_type: Optional[str]) -> Optional[str]:
    """Записать тип купона со smart-lab и сверить с нашим base.
    → 'mismatch_fixed' (у нас FIXED, там флоатер) | 'mismatch_floater'
    (у нас флоатер, там фикс) | None (сходится или сайт молчит).

    Молчание сайта (sl_type=None) не считаем расхождением и не затираем прошлый
    ответ: «не знаем» ничего не опровергает."""
    _ensure()
    isin = (isin or "").strip()
    with _lock, _conn() as c:
        row = c.execute("SELECT base FROM instruments WHERE isin=?", (isin,)).fetchone()
        if row is None:
            return None
        verdict = None
        if sl_type == "floater" and row["base"] == "FIXED":
            verdict = "mismatch_fixed"
        elif sl_type == "fixed" and row["base"] in _FLOAT_BASES_REG:
            verdict = "mismatch_floater"
        c.execute("UPDATE instruments SET sl_type=COALESCE(?, sl_type), sl_checked_at=?, "
                  "sl_mismatch=? WHERE isin=?",
                  (sl_type, _now(), 1 if verdict else 0, isin))
    return verdict


def clear_base(isin: str) -> bool:
    """Снять НАШ вердикт о базе купона: бумага возвращается в состояние «база
    неизвестна» и заново проходит конвейер (corpbonds → калибратор). Нужно,
    когда внешняя сверка показала, что вердикт неверен. Маржу не трогаем: она
    могла прийти из проспекта и от базы не зависит."""
    _ensure()
    with _lock, _conn() as c:
        n = c.execute("UPDATE instruments SET base=NULL, reviewed=0, updated_at=? "
                      "WHERE isin=? AND manual_locked=0", (_now(), (isin or "").strip())).rowcount
        c.execute("DELETE FROM enrich_seen WHERE isin=?", ((isin or "").strip(),))
    return bool(n)


def list_sl_stale(limit: int = 40) -> list[str]:
    """Порция бумаг на сверку со smart-lab: сначала ни разу не проверенные,
    затем самые давние. Внешний сайт — ходим понемногу за прогон."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin FROM instruments WHERE active=1 "
            "ORDER BY (sl_checked_at IS NOT NULL), sl_checked_at LIMIT ?", (limit,)).fetchall()
    return [r["isin"] for r in rows]


def list_sl_mismatch() -> list[dict]:
    """Бумаги, чей тип купона расходится со smart-lab — очередь на разбор."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, sl_type, sl_checked_at, "
            "manual_locked, source FROM instruments "
            "WHERE active=1 AND sl_mismatch=1 ORDER BY short_name").fetchall()
    return [dict(r) for r in rows]


def br_specs_all() -> dict:
    """{isin: {fixing_lag, fixing_lag_unit, coupon_mode}} слой bondresearch.ru.
    Приоритет в ref_data.coupon_formula: manual > bondresearch > парсер > калибратор."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, br_fixing_lag, br_coupon_mode, br_avg_window_days FROM instruments "
            "WHERE br_fixing_lag IS NOT NULL OR br_coupon_mode IS NOT NULL").fetchall()
    out = {}
    for r in rows:
        d = {}
        if r["br_fixing_lag"] is not None:
            d["fixing_lag"] = r["br_fixing_lag"]
            d["fixing_lag_unit"] = "cal"       # bondresearch публикует календарные дни
        if r["br_coupon_mode"]:
            d["coupon_mode"] = r["br_coupon_mode"]
        if r["br_avg_window_days"] is not None:
            d["avg_window_days"] = r["br_avg_window_days"]
        if d:
            out[r["isin"]] = d
    return out


def coupon_overrides_all() -> dict:
    """{isin: {купонные поля}} по бумагам с manual_locked=1 (явные правки СПРАВОЧНИКа).
    Мост в valuation: ref_data.coupon_formula накладывает это поверх Cbonds/проспекта,
    чтобы ручная тонкая настройка выпуска влияла на расчёт. Только непустые поля.
    Только locked — авто-sync значения НЕ вмешиваются в прайсинг (без регрессий)."""
    _ensure()
    cols = ",".join(_COUPON_OVERRIDE_COLS)
    with _conn() as c:
        rows = c.execute(
            f"SELECT isin,{cols} FROM instruments WHERE manual_locked=1").fetchall()
    out = {}
    for r in rows:
        ov = {k: r[k] for k in _COUPON_OVERRIDE_COLS if r[k] is not None}
        if ov:
            out[r["isin"]] = ov
    return out


# Поля, отсутствие которых ПЕРЕКЛЮЧАЕТ МЕТОДИКУ расчёта, а не просто снижает
# точность. Реестр отдаёт их как фолбэк для ВСЕХ бумаг (не только locked):
#   coupon_text — первоисточник спеки фиксинга; без него parse_prospectus_formula
#                 не зовётся, coupon_mode остаётся None и projected_ks_pct молча
#                 уходит на легаси форвард-проекцию (ВЭБ2Р-50: ±9 bps R-spread на
#                 одной и той же цене того же дня);
#   var_type    — признак «купон после оферты пересматривает эмитент»
#                 (ref_data.cut_at_offer); без него поток считается к погашению
#                 вместо оферты, и спред занижается почти вдвое (24 бумаги);
#   base, margin_bps — без них выпуск вообще непрайсуем.
#   margin_schedule — ручная лесенка маржи по номерам купонов. Конкурирующего
#                 источника у неё нет (парсер читает только coupon_text), а
#                 требовать ради неё manual_locked=1 значило бы ЗАМОРОЗИТЬ строку
#                 целиком (freeze-trap импорта xlsx) ради одного поля.
# Все они раньше приходили ТОЛЬКО из bondsearch-xlsx, который лежит вне git и вне
# тома данных: файл приезжает лишь rsync'ом деплоя.
_PRICING_FALLBACK_COLS = ("coupon_text", "var_type", "base", "margin_bps",
                          "margin_schedule")


def pricing_fallback_all() -> dict:
    """{isin: {поле: значение}} — реестровый ФОЛБЭК параметров прайсинга по всем
    бумагам, независимо от manual_locked. Только непустые поля.

    Отдельно от coupon_overrides_all: тот отдаёт ручные правки, которые ПЕРЕБИВАЮТ
    остальные слои (только locked). Здесь наоборот — самый НИЗКИЙ приоритет,
    закрытие дыры, когда источника выше нет вовсе."""
    _ensure()
    cols = ",".join(_PRICING_FALLBACK_COLS)
    with _conn() as c:
        rows = c.execute(f"SELECT isin,{cols} FROM instruments").fetchall()
    out = {}
    for r in rows:
        v = {k: r[k] for k in _PRICING_FALLBACK_COLS
             if r[k] is not None and r[k] != ""}
        if v:
            out[r["isin"]] = v
    return out


def list_catalog(only_active: bool = True, floaters_only: bool = False) -> list[dict]:
    """Полный справочник бумаг со ВСЕМИ параметрами (спарсенные + пропуски=None).
    Непрайсуемые (нет base/margin/maturity) идут первыми — их надо дозаполнить.
    priceable — флаг «хватает параметров для расчёта»."""
    _ensure()
    conds = []
    if only_active:
        conds.append("active=1")
    if floaters_only:
        conds.append("base IN ('KEYRATE','RUONIA')")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM instruments{where}").fetchall()
    try:
        from services.coupon_calib import parse_prospectus_formula as _ppf
    except Exception:
        _ppf = lambda t: None
    out = []
    for r in rows:
        d = {k: r[k] for k in _CATALOG_COLS}
        d["priceable"] = is_priceable(r)
        # ЭФФЕКТИВНАЯ спека фиксинга (read-only, в xlsx не выгружается):
        # ручной слой БД > парсер coupon_text > рантайм (калибратор/дефолт).
        # Пустые coupon_mode/fixing_lag в БД ≠ «спеки нет» — она резолвится
        # на лету; колонка показывает правду без записи в БД (запись плодит
        # заморозку, см. scripts/unfreeze_fixing_spec).
        d["spec_eff"] = None
        if r["base"] in ("KEYRATE", "RUONIA"):
            mode, lag, unit, src = r["coupon_mode"], r["fixing_lag"], r["fixing_lag_unit"], "ручной"
            if mode is None and lag is None and (r["br_coupon_mode"] or r["br_fixing_lag"] is not None):
                mode, lag, unit, src = r["br_coupon_mode"], r["br_fixing_lag"], "cal", "bondresearch"
            if mode is None and lag is None:
                ps = _ppf(r["coupon_text"] or "") or {}
                mode, lag, unit = ps.get("mode"), ps.get("lag"), ps.get("lag_unit")
                src = "парсер"
            if mode is None:
                d["spec_eff"] = "авто (калибратор/дефолт)"
            else:
                # point/avg_prev убраны из модели: показываем в единой
                # параметризации average+окно
                w = r["avg_window_days"] if src == "ручной" else (
                    r["br_avg_window_days"] if src == "bondresearch" else None)
                if mode == "point":
                    mode, w = "average", 1
                elif mode == "avg_prev":
                    mode, w = "average", w or r["coupon_period_days"]
                lag_s = "" if lag is None else f"·{lag}{'р' if unit == 'work' else ''}"
                w_s = f"·окно{w}" if w else ""
                d["spec_eff"] = f"{mode}{lag_s}{w_s} ({src})"
        out.append(d)
    # непрайсуемые вперёд, дальше по имени
    out.sort(key=lambda d: (d["priceable"], (d["short_name"] or d["isin"]).upper()))
    return out


def list_unreviewed() -> list[dict]:
    """Новые бумаги, у которых параметры ещё не подтверждены (для admin-ревью)."""
    _ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, maturity_date, source, first_seen "
            "FROM instruments WHERE reviewed=0 AND active=1 ORDER BY first_seen DESC").fetchall()
    return [dict(r) for r in rows]


# Окно «новый выпуск» для ручной проверки в Справочнике: бумага считается новой,
# пока с даты размещения (или, если её нет, с первого появления в реестре) прошло
# не больше стольких дней. Шире окна перепопыток обогащения (_FRESH_ISSUE_DAYS):
# параметры свежего выпуска дозревают в источниках неделями, а глаз админа нужен
# всё это время.
NEW_ISSUE_DAYS = 30


def list_new_issues(days: int = NEW_ISSUE_DAYS, unchecked_only: bool = False) -> list[dict]:
    """Свежие выпуски — кандидаты на ручную проверку параметров.

    «Свежий» = issue_date не старше days (фолбэк — first_seen: у бумаг без даты
    размещения единственный признак новизны — день, когда её нашла дискавери).
    unchecked_only=True оставляет только те, что админ ещё не подтвердил
    (reviewed=0) — это и есть счётчик на кнопке «Справочник».
    """
    _ensure()
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM instruments WHERE active=1 "
            "AND COALESCE(substr(issue_date,1,10), substr(first_seen,1,10)) >= ? "
            "ORDER BY COALESCE(issue_date, first_seen) DESC", (cutoff,)).fetchall()
    out = []
    for r in rows:
        if unchecked_only and r["reviewed"]:
            continue
        out.append({"isin": r["isin"], "short_name": r["short_name"], "base": r["base"],
                    "margin_bps": r["margin_bps"], "maturity_date": r["maturity_date"],
                    "issue_date": r["issue_date"], "first_seen": r["first_seen"],
                    "source": r["source"], "reviewed": bool(r["reviewed"]),
                    "priceable": is_priceable(r)})
    return out


def new_issue_isins(days: int = NEW_ISSUE_DAYS) -> list[str]:
    """ISIN свежих выпусков, новые первыми — приоритет очередям обогащения."""
    return [r["isin"] for r in list_new_issues(days)]


def sync_from_sources(nrd_items: list[dict] | None = None,
                      cbonds: dict | None = None,
                      manual: dict | None = None) -> dict:
    """Наполнить/обновить реестр из готовых источников (чистая, без сети — сеть
    собирает вызывающий, см. sync_instruments в poller). Приоритет расчётных полей:
    manual > Cbonds > замороженный NRD. Возвращает статистику {new, updated, total}.

    nrd_items — строки замороженного nrd_universe_cache (isin/name/base_rate_type/
                spread_issue_bps/maturity_date/rating);
    cbonds     — {isin: {base, margin_bps, name, freq, var_type, day_count}} (ref_data.load_cbonds);
    manual     — {isin: {...оверрайды}} (ref_data.load_manual)."""
    _ensure()
    cbonds = cbonds or {}
    manual = manual or {}
    # ручной слой: только реальные записи (dict), служебные ключи (_README) — вон
    manual = {k: v for k, v in manual.items()
              if not k.startswith("_") and isinstance(v, dict) and v}
    # объединённое множество ISIN из всех источников
    isins: set[str] = set()
    nrd_by = {}
    for it in nrd_items or []:
        i = (it.get("isin") or "").strip()
        if i:
            isins.add(i)
            nrd_by[i] = it
    isins.update(k.strip() for k in cbonds if k and not k.startswith("_"))
    isins.update(k.strip() for k in manual if k)

    stats = {"new": 0, "updated": 0, "skipped_locked": 0, "total": len(isins)}
    for isin in isins:
        n = nrd_by.get(isin, {})
        cb = cbonds.get(isin, {})
        # база/маржа: Cbonds авторитетнее (сверено 317/326 ±5бп), NRD — фолбэк
        base = cb.get("base") or n.get("base_rate_type")
        margin = cb.get("margin_bps")
        if margin is None:
            margin = n.get("spread_issue_bps")
        freq = cb.get("freq")
        row = {
            "isin": isin,
            "short_name": cb.get("name") or n.get("name"),
            "base": base,
            "margin_bps": int(margin) if margin is not None else None,
            # maturity/issue/face — bondsearch (Cbonds) теперь их тоже несёт, фолбэк NRD/MOEX
            "maturity_date": cb.get("maturity_date") or n.get("maturity_date"),
            "issue_date": cb.get("issue_date"),
            "face_value": cb.get("face_value"),
            "coupons_per_year": int(freq) if freq else None,
            # 365/freq — фолбэк; фактический период из графика (discovery) не трогаем
            "coupon_period_days": (round(365 / freq) if freq
                                   and not (get(isin) or {}).get("coupon_period_days")
                                   else None),
            "day_count": cb.get("day_count"),
            "var_type": cb.get("var_type"),
            "coupon_text": cb.get("coupon_text"),   # формула купона (для СПРАВОЧНИКа + проспект-парс)
            # рейтинг: выгрузка знает свежий выпуск раньше corpbonds/НРД, но
            # это СНИМОК на дату файла — им нельзя перебивать живой драйн
            # (set_rating), иначе понижение рейтинга откатывалось бы каждым
            # синком до следующей выгрузки. Только заполнение пропуска.
            "rating": n.get("rating") or (cb.get("rating")
                                          if not (get(isin) or {}).get("rating") else None),
        }
        res = upsert(row, source="cbonds" if cb else "nrd_frozen")
        stats[res if res in stats else "updated"] = stats.get(res, 0) + 1
    # ручной слой поверх (lock=True — sync впредь не затрёт); manual уже отфильтрован
    for isin, p in manual.items():
        try:
            set_manual(isin.strip(), p, lock=True)
        except Exception as e:
            # ПО СТРОКЕ, а не всем скопом: у set_manual появилась валидация
            # (coupons_per_year), и одна плохая ручная запись иначе обрывала
            # импорт целиком — молча, на середине
            _log.warning("ручной слой %s: %s", isin, e)
            stats["manual_errors"] = stats.get("manual_errors", 0) + 1
    return stats


# Пере-проверка ISIN без bondization-данных (is_floater IS NULL): свежий выпуск
# мог не иметь опубликованного графика в момент первой проверки — даём шанс позже.
_DISCOVERY_NULL_TTL_DAYS = 1
# Вердикт «фикс» (is_floater=0) тоже перечекиваем, но редко: у свежего выпуска
# на момент 1-й проверки MOEX мог ещё не опубликовать будущие незафиксированные
# периоды → флоатер навсегда застревал как «фикс» (тихий отказ: бумаги просто
# нет в дашборде). 3446 кандидатов / 90 дн — копеечная перепроверка.
_DISCOVERY_FIXED_TTL_DAYS = 90


def discovery_pending(candidates: list[str], limit: int) -> list[str]:
    """Из candidates — ISIN, которых нет НИ в реестре инструментов, НИ в negative-
    кэше discovery_seen со СВЕЖИМ результатом. Порядок candidates сохраняется
    (приоритет вероятных флоатеров). Гарантирует прогресс: «флоатер» (=1, уже в
    instruments) не перечекивается; «фикс» (=0) перечекивается спустя
    _DISCOVERY_FIXED_TTL_DAYS (у свежего выпуска мог не быть незафикс. периодов);
    «нет данных» (NULL) — спустя _DISCOVERY_NULL_TTL_DAYS (bondization появляется).
    Без negative-кэша структурные ноты без графика забивали бы cap вечно."""
    _ensure()
    if not candidates:
        return []
    now = datetime.now(timezone.utc)
    cutoff_null = (now - timedelta(days=_DISCOVERY_NULL_TTL_DAYS)).isoformat()
    cutoff_fixed = (now - timedelta(days=_DISCOVERY_FIXED_TTL_DAYS)).isoformat()
    with _conn() as c:
        known = {r[0] for r in c.execute("SELECT isin FROM instruments")}
        skip = {r[0] for r in c.execute(
            "SELECT isin FROM discovery_seen WHERE is_floater=1 "
            "OR (is_floater=0 AND checked_at >= ?) "
            "OR (is_floater IS NULL AND checked_at >= ?)",
            (cutoff_fixed, cutoff_null))}
    out: list[str] = []
    for i in candidates:
        if i in known or i in skip:
            continue
        out.append(i)
        if len(out) >= limit:
            break
    return out


def mark_discovery_seen(isin: str, is_floater: Optional[bool]) -> None:
    """Записать результат bondization-проверки в negative-кэш: True=флоатер (заведён
    в instruments), False=фикс/прочее (значимо — не перечекивать), None=нет данных
    графика (перечекнуть после TTL). Дискавери не гоняет решённые каждый прогон."""
    _ensure()
    isin = (isin or "").strip()
    if not isin:
        return
    val = None if is_floater is None else (1 if is_floater else 0)
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO discovery_seen(isin, is_floater, checked_at) "
                  "VALUES(?,?,?)", (isin, val, _now()))


# TTL перепопытки corpbonds-обогащения по исходу прошлой попытки (дни).
# not_found/nodata — corpbonds доливает свежие выпуски со временем, перечекиваем;
# exotic — вердикт парсера детерминирован, перечек редкий (до версионирования
# парсера); filled — бумага уходит из очередей сама, короткий guard от зацикла.
_ENRICH_TTL_DAYS = {"not_found": 14, "nodata": 14, "exotic": 30, "filled": 7}

# СВЕЖИЙ ВЫПУСК — отдельный режим перепопытки. corpbonds доливает новые бумаги
# с задержкой в недели, а TTL 14 дней всё это время держал флоатер НЕВИДИМЫМ:
# без base/margin бумага непрайсуема, и universe_rows(only_priceable=True) её
# выкидывает. Случай, на котором поймано: РЖД 1Р-54R размещена 13.08, вердикт
# nodata в тот же день, следующая автопопытка пришлась бы на 27.08 — две недели
# «бумаги нет», хотя реестр знал о ней с первого дня.
_FRESH_ISSUE_DAYS = 45
_FRESH_ISSUE_TTL_DAYS = 1
# Ежедневный повтор осмыслен только там, где вердикт про ИСТОЧНИК («ещё не знает
# бумагу»), а не про саму бумагу: exotic/filled от возраста выпуска не зависят.
_FRESH_RETRY_RESULTS = ("not_found", "nodata")


def mark_enrich_attempt(isin: str, result: str, parser_ver: int | None = None) -> None:
    """Записать исход corpbonds-попытки в negative-кэш enrich_seen. Без него
    очередь голодала: fromkeys()[:cap] каждый день дёргал одни и те же первые
    60 ISIN (стабильный rowid-порядок), и бумаги, которых нет на corpbonds,
    вечно блокировали хвост (355 incomplete при cap 60 = хвост недостижим)."""
    _ensure()
    isin = (isin or "").strip()
    if not isin:
        return
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO enrich_seen(isin, result, attempted_at, parser_ver) "
                  "VALUES(?,?,?,?)", (isin, result, _now(), parser_ver))


def enrich_info(isin: str) -> Optional[dict]:
    """Последняя corpbonds-попытка обогащения (negative-кэш enrich_seen):
    {result, attempted_at, parser_ver} | None. Для паспорта бумаги (провенанс)."""
    _ensure()
    with _conn() as c:
        r = c.execute("SELECT result, attempted_at, parser_ver FROM enrich_seen "
                      "WHERE isin=?", (isin,)).fetchone()
    return dict(r) if r else None


def enrich_pending(candidates: list[str], limit: int,
                   parser_ver: int | None = None) -> list[str]:
    """Из candidates — до limit ISIN на corpbonds-обогащение. Ротация: никогда
    не пробованные — первыми, затем по возрасту прошлой попытки (старейшие
    вперёд); свежие попытки (внутри TTL по исходу) пропускаются. Каждый прогон
    двигается по хвосту очереди, а не топчет голову.
    parser_ver: попытка более старой версии парсера считается протухшей сразу —
    после фикса парсера ложные EXOTIC/nodata перечекиваются, не дожидаясь TTL."""
    _ensure()
    if not candidates:
        return []
    now = datetime.now(timezone.utc)
    with _conn() as c:
        seen = {r["isin"]: r for r in c.execute("SELECT * FROM enrich_seen")}
        # дата размещения отличает «свежий выпуск, источник его ещё не знает» от
        # «старая бумага, которой на corpbonds нет вовсе». Неизвестная дата = НЕ
        # свежий: у сидового импорта issue_date пуст, и без этой осторожности
        # весь бэклог (500+) считался бы новым и топтал квоту каждый день.
        issued = {r["isin"]: r["issue_date"] for r in c.execute(
            "SELECT isin, issue_date FROM instruments WHERE issue_date IS NOT NULL")}
    today = now.date()

    def _fresh_issue(isin: str) -> bool:
        try:
            d = date.fromisoformat((issued.get(isin) or "")[:10])
        except ValueError:
            return False
        return 0 <= (today - d).days <= _FRESH_ISSUE_DAYS

    def _fresh(isin: str, r) -> bool:
        # вердикт устаревшего парсера — перечекнуть немедленно; только исходы,
        # где парсер решал (exotic/nodata): not_found/filled от версии не зависят
        if (parser_ver is not None and r["result"] in ("exotic", "nodata")
                and (r["parser_ver"] or 0) < parser_ver):
            return False
        ttl = _ENRICH_TTL_DAYS.get(r["result"], 14)
        if r["result"] in _FRESH_RETRY_RESULTS and _fresh_issue(isin):
            ttl = min(ttl, _FRESH_ISSUE_TTL_DAYS)
        try:
            return (now - datetime.fromisoformat(r["attempted_at"])).days < ttl
        except (ValueError, TypeError):
            return False

    never = [i for i in candidates if i not in seen]
    # свежие выпуски вперёд остального хвоста: иначе ежедневная перепопытка
    # ничего не даёт — квоту (30 incomplete за прогон) съедает ротация старого
    # бэклога, и новая бумага снова ждёт.
    retry = sorted((i for i in candidates if i in seen and not _fresh(i, seen[i])),
                   key=lambda i: (not _fresh_issue(i), seen[i]["attempted_at"]))
    return (never + retry)[:limit]


def non_fixed_isins() -> set[str]:
    """ISIN, которые НЕЛЬЗЯ считать фикс-бумагами (исключение для вкладки ФИКСЫ):
    все активные записи реестра, кроме подтверждённых base='FIXED' — base NULL
    (флоатер без параметров) и EXOTIC (вне линейной модели) — это флоатеры,
    просто непрайсуемые; плюс discovery_seen.is_floater=1 (подтверждён
    bondization'ом). Фильтр только по KEYRATE/RUONIA пропускал их в ФИКСЫ, где
    у флоатера зафиксированный текущий купон cp>0 маскирует его под фикс и
    поток режется на первом value=None → ложный «YTM к оферте»."""
    _ensure()
    with _conn() as c:
        a = {r[0] for r in c.execute(
            "SELECT isin FROM instruments WHERE active=1 "
            "AND (base IS NULL OR base != 'FIXED')")}
        b = {r[0] for r in c.execute(
            "SELECT isin FROM discovery_seen WHERE is_floater=1")}
    return a | b


def _fresh_incomplete(rows) -> list[dict]:
    """Непрайсуемые выпуски моложе _FRESH_ISSUE_DAYS — поимённо, новые первыми.
    Возраст в днях от размещения: он и есть мера «сколько бумаги нет в универсе».
    Бумаги без даты размещения сюда не попадают — их возраст неизвестен, и
    сидовый бэклог (сотни строк) утопил бы список."""
    today = datetime.now(timezone.utc).date()
    out = []
    for r in rows:
        try:
            d = date.fromisoformat((r["issue_date"] or "")[:10])
        except (ValueError, TypeError):
            continue
        age = (today - d).days
        if 0 <= age <= _FRESH_ISSUE_DAYS:
            out.append({"isin": r["isin"], "name": r["short_name"] or r["isin"],
                        "issue_date": r["issue_date"][:10], "age_days": age,
                        "has_base": bool(r["base"]), "has_margin": r["margin_bps"] is not None})
    out.sort(key=lambda x: x["age_days"])
    return out


def queue_stats() -> dict:
    """Размеры очередей обработки + возраст головы очереди (для /status).
    Видимость голодания: очередь, которая не сходится, копится и стареет —
    без этих чисел отказ тихий (бумага просто не появляется в дашборде).
    oldest_days считаем по updated_at: голова, которую никто не трогает,
    стареет; при живой ротации возраст головы ~= период полного прохода."""
    _ensure()
    now = datetime.now(timezone.utc)

    def _age_days(iso: Optional[str]) -> Optional[int]:
        try:
            return (now - datetime.fromisoformat(iso)).days
        except (ValueError, TypeError):
            return None

    with _conn() as c:
        rows = c.execute(
            "SELECT isin, short_name, base, margin_bps, maturity_date, issue_date, "
            "updated_at, reviewed, margin_check_pp, manual_locked "
            "FROM instruments WHERE active=1").fetchall()
        enrich_seen = {r["isin"] for r in c.execute("SELECT isin FROM enrich_seen")}
    incomplete = [r for r in rows
                  if not is_priceable(r) and (r["base"] in ("KEYRATE", "RUONIA") or r["base"] is None)]
    suspect = [r for r in rows if r["margin_check_pp"] is not None
               and abs(r["margin_check_pp"]) > _SUSPECT_PP]
    exotic = [r for r in rows if r["base"] == "EXOTIC" and not r["manual_locked"]]
    unreviewed = [r for r in rows if not r["reviewed"]]

    def _oldest(rs):
        ages = [a for r in rs if (a := _age_days(r["updated_at"])) is not None]
        return max(ages) if ages else None

    return {
        # never_tried: сколько из очереди ещё ни разу не ходило в corpbonds —
        # честный индикатор сходимости (updated_at бампается листинг-рефрешем)
        "incomplete": {"n": len(incomplete), "oldest_days": _oldest(incomplete),
                       "never_tried": sum(1 for r in incomplete if r["isin"] not in enrich_seen),
                       # ПОИМЕННО свежие: счётчик не отвечает на вопрос «где моя
                       # бумага?». Непрайсуемая = невидимая в универсе, и до
                       # появления этого списка единственным признаком было
                       # «я не вижу выпуск, который жду» (случай РЖД 1Р-54R:
                       # размещена 13.08, замечена руками 25.08).
                       "fresh": _fresh_incomplete(incomplete)},
        "suspect": {"n": len(suspect), "oldest_days": _oldest(suspect)},
        # ставка менялась на прошлой оферте, впереди ещё одна — горизонт оценки
        # под вопросом (см. list_offer_reset)
        "offer_reset": {"n": len(list_offer_reset()), "oldest_days": None},
        "exotic": {"n": len(exotic), "oldest_days": _oldest(exotic)},
        "unreviewed": {"n": len(unreviewed), "oldest_days": _oldest(unreviewed)},
        "manual_locked": sum(1 for r in rows if r["manual_locked"]),
    }


def count() -> dict:
    _ensure()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM instruments WHERE active=1").fetchone()[0]
        floaters = c.execute(
            "SELECT COUNT(*) FROM instruments WHERE active=1 AND base IN ('KEYRATE','RUONIA')"
        ).fetchone()[0]
        unrev = c.execute("SELECT COUNT(*) FROM instruments WHERE reviewed=0 AND active=1").fetchone()[0]
        priceable = sum(1 for r in c.execute(
            "SELECT base, margin_bps, maturity_date FROM instruments WHERE active=1").fetchall()
            if is_priceable(r))
        suspect = c.execute(
            "SELECT COUNT(*) FROM instruments WHERE active=1 AND margin_check_pp IS NOT NULL "
            "AND ABS(margin_check_pp) > ?", (_SUSPECT_PP,)).fetchone()[0]
    new_unchecked = len(list_new_issues(unchecked_only=True))
    return {"total": total, "floaters": floaters, "unreviewed": unrev,
            "priceable": priceable, "incomplete": floaters - priceable,
            "suspect": suspect, "offer_reset": len(list_offer_reset()),
            # свежие выпуски без подтверждения параметров — значок на кнопке
            # «Справочник» и фильтр «новые» на самой странице
            "new_issues": new_unchecked}
