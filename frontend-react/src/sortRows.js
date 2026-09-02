/**
 * Сортировка строк монитора с ПАМЯТЬЮ ПОЗИЦИИ.
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
