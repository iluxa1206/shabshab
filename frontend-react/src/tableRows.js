/**
 * Порядок и состав строк монитора, устойчивые к пересчёту спредов.
 *
 * Сортировка с ПАМЯТЬЮ ПОЗИЦИИ.
 *
 * Спред стороны гаснет на каждом движении цены — число, посчитанное к прошлой
 * цене, к новой не относится, и до следующего такта движка в ячейке честный
 * прочерк. При сортировке по такому столбцу пустое значение уезжало в конец
 * списка, и строки прыгали на каждом тике: читать таблицу в этот момент
 * невозможно, а бумага, за которой следишь, исчезала из поля зрения.
 *
 * Сортируем по ПОСЛЕДНЕМУ ИЗВЕСТНОМУ числу: место сохраняется, а в ячейке
 * остаётся прочерк, пока спред не посчитан заново. В конец уходят только те,
 * что не считались ни разу.
 */

/** memo — { key, map }, живёт между вызовами (useRef). Возвращает тот же массив. */
export function sortRows(rows, key, dir, memo, valueOf) {
  const m = dir === "asc" ? 1 : -1;
  if (memo.key !== key) {          // сменили столбец — прошлая память не о нём
    memo.key = key;
    memo.map = new Map();
  }
  const seen = memo.map;
  const pos = new Map();
  for (const r of rows) {
    const v = valueOf(r);
    if (v != null) seen.set(r.isin, v);
    pos.set(r.isin, v != null ? v : seen.get(r.isin) ?? null);
  }
  // запомненное живёт, только пока строка в списке: ушла из фильтра — ушла и
  // память о ней, иначе карта копила бы весь рынок
  for (const isin of [...seen.keys()]) if (!pos.has(isin)) seen.delete(isin);
  rows.sort((a, b) => {
    const x = pos.get(a.isin), y = pos.get(b.isin);
    if (x == null && y == null) return 0;
    if (x == null) return 1;             // не считалась ни разу — в конец
    if (y == null) return -1;
    if (typeof x === "string") return x.localeCompare(y) * m;
    return (x - y) * m;
  });
  return rows;
}


/**
 * Окно R-spread: строка не исчезает, пока спред считается.
 *
 * Пустое значение выкидывало её из списка на каждом движении цены — бумага
 * мигала, а с ней прыгало всё, что ниже. Держим по последнему известному
 * числу; в самой ячейке оно приглушено, пока движок не пересчитает.
 *
 * memo — Map(isin → последний спред), живёт между вызовами (useRef).
 */
export function filterBySpread(rows, from, to, memo, valueOf = (r) => r.yield_over_index_bps) {
  const hasFrom = Number.isFinite(from), hasTo = Number.isFinite(to);
  if (!hasFrom && !hasTo) return rows;
  const out = rows.filter((r) => {
    const v = valueOf(r);
    if (v != null) memo.set(r.isin, v);
    const x = v != null ? v : memo.get(r.isin);
    if (x == null) return false;          // не считалась ни разу
    if (hasFrom && x < from) return false;
    return !(hasTo && x > to);
  });
  // память живёт, пока строка на экране: иначе карта копила бы весь рынок
  const alive = new Set(rows.map((r) => r.isin));
  for (const isin of [...memo.keys()]) if (!alive.has(isin)) memo.delete(isin);
  return out;
}

// Порог ликвидности по ADV (среднедневной оборот за месяц, ₽ в строке — млн ₽
// в инпуте). mode: "gte" — оборот не меньше порога (отсечь неликвид), "lte" —
// не больше (найти именно тонкие бумаги). Строки без ADV при заданном пороге
// прячем: прочерк — это и есть отсутствие оборота, пускать его в «ликвидные»
// нельзя, а в «тонкие» — врать числом, которого нет.
export function filterByAdv(rows, minMln, mode = "gte") {
  if (!Number.isFinite(minMln)) return rows;
  const lim = minMln * 1e6;
  return rows.filter((r) => {
    const v = r.adv_1m_rub;
    if (v == null) return false;
    return mode === "lte" ? v <= lim : v >= lim;
  });
}
