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

// Короткие ярлыки базы для плотных мест (таблица, карточка): RU / КС.
// Полные названия остаются только в поясняющих текстах и модуле кривых.
export const baseLabel = (b) => (b === "RUONIA" ? "RU" : b === "KEYRATE" ? "КС" : b || DASH);

// Купонов в год. Фактический период купона (считается из реального графика выплат)
// авторитетнее декларированной частоты из справочников — тот же приоритет, что на бэке.
export const couponsPerYear = (periodDays, declared) => {
  const p = Number(periodDays);
  if (p > 0) return Math.max(1, Math.min(365, Math.round(365 / p)));
  const d = Number(declared);
  return d > 0 ? Math.round(d) : null;
};

// Семантика для сканируемости: DM выше/положительный = дёшево (up), ниже = дорого (down).
export function dmColor(dm) {
  if (dm == null) return { color: "var(--mut-2)" };
  return { color: dm >= 0 ? "var(--up)" : "var(--down)" };
}
