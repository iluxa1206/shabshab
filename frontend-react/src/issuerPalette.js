// Палитра «цвет = эмитент» — общая для аналитики флоатеров и фиксов. Копий быть
// не должно: один и тот же эмитент обязан краситься одинаково на обеих вкладках.

// рейтинг-цвета заняты бакетами (format.js), поэтому у эмитентов своя палитра
export const ICOLORS = ["#4f9cf9", "#f9a04f", "#3fbf7f", "#e05c66",
                        "#b07cf9", "#3fc6c6", "#d4b83f", "#f97cc0"];
export const OTHER_COLOR = "var(--mut-2)";

/** Карта «эмитент → цвет»: ICOLORS достаётся самым представленным в наборе,
 *  хвост уходит серым. Цветов восемь — больше глаз всё равно не различает, а
 *  раскрашивать две сотни эмитентов радугой бессмысленно.
 *  keyFn — как достать имя эмитента из строки витрины (у витрин оно разное). */
export function issuerColors(rows, keyFn) {
  const cnt = new Map();
  for (const b of rows) {
    const k = keyFn(b);
    if (k) cnt.set(k, (cnt.get(k) || 0) + 1);
  }
  const top = [...cnt.entries()]
    .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))
    .slice(0, ICOLORS.length);
  return new Map(top.map(([k], i) => [k, ICOLORS[i]]));
}
