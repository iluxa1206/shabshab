// Общий монитор облигаций: чистые функции над строками двух классов.
//
// Строка флоатера — как отдаёт /api/bonds. Строка фикса — как отдаёт
// /api/fixed после слияния с живыми данными (useFixedFeed): НАТИВНЫЕ имена
// (bid, ask, wap_pct, ytm, put_date…), потому что правила живого слияния
// написаны под них. Общий конвейер фильтров и сортировка работают на именах
// флоатера, поэтому к строке фикса ДОКЛАДЫВАЮТСЯ алиасы — производные от
// нативных полей, считаются заново при каждом слиянии и устареть не могут.
// Ячейки таблицы выбирают поля по kind (см. components/unifiedCols.jsx).

import { horizonDate, horizonYears } from "./horizon.js";
import { fixedCurrency } from "./components/fixed/currency.js";

// Кэш по ссылке исходной строки: пока фид не пересоздал строку, алиасная копия
// та же — иначе memo(BondRow) бесполезен и таблица перерисовывается целиком.
const _aliased = new WeakMap();

/** Алиасы имён флоатера у строки фикса. Исходный объект не меняется. */
export function withFixedAliases(row) {
  if (!row || row.kind !== "fixed") return row;
  const hit = _aliased.get(row);
  if (hit) return hit;
  const out = {
    ...row,
    // у выпуска без оферты put_date пуст — тогда горизонт один, и указывать
    // preferred_horizon незачем (horizonDate берёт put_date || maturity_date)
    short_name: row.short_name ?? row.name,
    emitter_name: row.emitter_name ?? row.issuer,
    is_ofz: row.cls === "ofz",
    base_rate_type: "FIXED",
    bid_price_pct: row.bid ?? null,
    ask_price_pct: row.ask ?? null,
    wap_price_pct: row.wap_pct ?? null,
    yield_xirr_pct: row.ytm ?? null,
    offer_date: row.put_date ?? null,
    offer_kind: row.put_date ? "put" : null,
  };
  _aliased.set(row, out);
  return out;
}

/** Дюрация фикса (модифицированная) либо срок до горизонта флоатера, лет.
 *  Одна колонка, но величины разные — ячейка подписывает, какая именно. */
export function durValue(b) {
  if (b.kind === "fixed") return b.mod_dur ?? null;
  const y = horizonYears(b);
  return y != null && y >= 0 ? y : null;
}

/** Значение для сортировки колонки общего монитора. */
export function unifiedSortValue(b, key) {
  if (key === "maturity_date") return horizonDate(b);
  if (key === "dur") return durValue(b);
  if (key === "coupon") return b.kind === "fixed" ? (b.coupon_pct ?? null) : (b.spread_issue_bps ?? null);
  return b[key];
}

/**
 * Дефолтные отсечки общего монитора. Структурные ноты и порог ликвидности —
 * ТОЛЬКО на фиксы (у флоатеров свой, давно сложившийся набор фильтров); валюта
 * номинала — на оба класса (у флоатера её отдаёт бэк, нет поля — рубль).
 *  - hideStructured: купон ≤ 1 % у не-ОФЗ (флаг structured с бэка) — спред
 *    структурной ноты не про рынок ставок и забивает верх сортировки;
 *  - advMinRub: порог ADV, ₽; бумага без посчитанного ADV при заданном
 *    пороге скрыта (как у общего фильтра ADV — неликвид не пролезает);
 *  - currencies: пустой массив = все валюты (в разделе «fx» — все нерублёвые);
 *  - ccyMode: «rub» | «fx» — раздел меню по валюте номинала.
 */
export function passesFixedDefaults(b, { hideStructured, advMinRub, currencies, ccyMode }) {
  const c = fixedCurrency(b);
  if (ccyMode) {
    // раздел меню: «rub» — только рубли, «fx» — только нерублёвые (из них выбранные
    // чипами, пусто = все)
    if (ccyMode === "rub" ? c !== "RUB" : c === "RUB") return false;
    if (ccyMode === "fx" && currencies?.length && !currencies.includes(c)) return false;
  } else if (currencies?.length && !currencies.includes(c)) return false;
  if (b.kind !== "fixed") return true;
  if (hideStructured && b.structured) return false;
  if (advMinRub > 0 && !((b.adv_1m_rub ?? -1) >= advMinRub)) return false;
  return true;
}
