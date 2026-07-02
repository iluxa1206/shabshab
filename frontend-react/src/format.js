// единый десятичный разделитель — запятая (ru), тысячи — пробел
export const fmt = {
  pct: (v, d = 2) => (v == null ? null : v.toFixed(d).replace(".", ",")),
  num: (v, d = 2) =>
    v == null ? null : v.toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }),
  bps: (v) => (v == null ? null : (v > 0 ? "+" : "") + Math.round(v)),
  date: (s) => (s == null ? null : s.split("-").reverse().join(".")),
  signed: (v, d = 2) => (v == null ? null : (v > 0 ? "+" : "") + v.toFixed(d).replace(".", ",")),
  // срок в годах: <1г в месяцах, иначе годы
  yrs: (v) => (v == null ? null : v < 1 ? Math.round(v * 12) + "м" : v.toFixed(1).replace(".", ",") + "г"),
  days: (v) => (v == null ? null : v + "д"),
};

// прочерк-плейсхолдер как React-узел
export const DASH = "—";
export const orDash = (s) => (s == null || s === "" ? DASH : s);

// Семантика для сканируемости: DM выше/положительный = дёшево (up), ниже = дорого (down).
export function dmColor(dm) {
  if (dm == null) return { color: "var(--mut-2)" };
  return { color: dm >= 0 ? "var(--up)" : "var(--down)" };
}
// vs Fair: рынок дешевле НРД (v<0) = up; дороже (v>0) = down.
export function vsFairColor(v) {
  if (v == null) return { color: "var(--mut-2)" };
  return { color: v < 0 ? "var(--up)" : "var(--down)" };
}
