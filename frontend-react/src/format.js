// единый десятичный разделитель — запятая (ru), тысячи — пробел
export const fmt = {
  pct: (v, d = 2) => (v == null ? null : v.toFixed(d).replace(".", ",")),
  num: (v, d = 2) =>
    v == null ? null : v.toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d }),
  // Спреды в bps: знак «+» НЕ рисуем — положительных подавляющее большинство,
  // сотня плюсов в колонке шумит и не несёт информации. Знак несут цвет
  // (dmColor: up/down) и минус у отрицательных.
  bps: (v) => (v == null ? null : String(Math.round(v))),
  // Отклонение спреда в bps — тут знак НУЖЕН: колонка про «шире/уже базы», и
  // плюс отличает её от самой базы под ней. Округление до целого делается ДО
  // выбора знака, иначе −0,4 рисовалось бы как «−0».
  devBps: (v) => {
    if (v == null) return null;
    const n = Math.round(v);
    return n === 0 ? "0" : (n > 0 ? "+" : "") + n;
  },
  date: (s) => (s == null ? null : s.split("-").reverse().join(".")),
  signed: (v, d = 2) => (v == null ? null : (v > 0 ? "+" : "") + v.toFixed(d).replace(".", ",")),
  // срок в годах: <1г в месяцах, иначе годы
  yrs: (v) => (v == null ? null : v < 1 ? Math.round(v * 12) + "м" : v.toFixed(1).replace(".", ",") + "г"),
  days: (v) => (v == null ? null : v + "д"),
  // ЕДИНАЯ единица денег во всём интерфейсе — МИЛЛИОНЫ рублей. Подпись «млн ₽»
  // живёт один раз в шапке колонки/поля, а в ячейках стоит голое число: рабочая
  // единица тикета на этом рынке — миллион, и смесь «900 ₽ / 45 тыс / 1,05 млрд»
  // в одной колонке невозможно сравнить глазами.
  //   1 050,0 · 375,0 · 12,5 · 0,045 · 0,001
  // От миллиона и выше хватает одного знака, мелочь показываем тремя — иначе
  // мелкие принты схлопнулись бы в «0,0» и стали неразличимы.
  mln: (v) => {
    if (v == null) return null;
    if (v === 0) return "0";
    const m = v / 1e6;
    const a = Math.abs(m);
    if (a < 0.0005) return v > 0 ? "<0,001" : ">−0,001";
    const d = a >= 1 ? 1 : 3;
    return m.toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
  },
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

// ЕДИНАЯ база срока на фронте: календарные годы (365.25), а не торговые — цифра
// рядом с датой это СРОК, а не duration. Своих копий этой арифметики по
// компонентам быть не должно: их было три (format/CalcModule/App), и одна и та
// же дата давала разные числа на разных экранах.
const YEAR_MS = 365.25 * 864e5;

// Лет до даты числом (без округления): ось графика, сравнения, пороги.
// Прошедшая дата → отрицательное значение, отсутствующая → null.
// Принимает и «2032-03-17», и «2032-03-17 00:00:00» (лента отдаёт со временем).
export const yearsToNum = (iso) => {
  if (!iso) return null;
  const t = Date.parse(String(iso).slice(0, 10) + "T00:00:00Z");
  return Number.isFinite(t) ? (t - Date.now()) / YEAR_MS : null;
};

// Лет до даты, одна десятая — подпись рядом с датой. Прошедшая → null (скобок не будет).
export const yearsTo = (iso) => {
  const y = yearsToNum(iso);
  return y == null || y < 0 ? null : y.toFixed(1);
};

// Обратная операция: граница окна срока в ISO-дату (фильтр «от N лет до M»).
export const yearsToIso = (y) => new Date(Date.now() + y * YEAR_MS).toISOString().slice(0, 10);

// Цвет рейтингового бакета — общий словарь для фильтров, таблиц и графиков.
export const RT_COLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BELOW: "var(--rt-bb)", NR: "var(--mut-2)",
};

// РЕЙТИНГОВЫЙ БАКЕТ — одно правило на весь фронт. Копий было четыре
// (AnalyticsPanel, CalcModule, FixedAnalytics, ratingColor) с ТРЕМЯ разными
// ответами: CCC уходил то в B, то в NR, то в BELOW — одна бумага попадала в
// разные корзины на соседних экранах.
//
// Ниже B отдельных корзин нет: CCC/CC/C/D — это тот же «глубокий хай-йилд»,
// их считаем B. Пустое/нераспознанное — NR.
export const RT_BUCKETS = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"];

// Палитра бакетов для ГРАФИКОВ (семь цветов). Таблицы и фильтр схлопывают
// BB/B в один чип «BB↓» — их цвет берётся из RT_COLOR.BELOW.
export const RT_BUCKET_COLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BB: "var(--rt-bb)", B: "var(--rt-b)", NR: "var(--mut-2)",
};

export function ratingBucket(rating) {
  const r = (rating || "").trim().toUpperCase();
  if (!r) return "NR";
  if (RT_BUCKETS.includes(r) && r !== "NR") return r;
  return /^(CCC|CC|C|D)$/.test(r) ? "B" : "NR";
}

// Подходит ли рейтинг под выбор чипов фильтра (AAA / AA / A / BBB / BELOW / NR).
// BELOW — «BB↓», то есть любой бакет ниже BBB.
export function ratingMatches(rating, sel) {
  if (!sel || !sel.length) return true;
  const b = ratingBucket(rating);
  return sel.some((k) => (k === "BELOW" ? b === "BB" || b === "B" : k === b));
}

// Цвет КОНКРЕТНОГО рейтинга: всё ниже BBB схлопывается в бакет BELOW (в фильтре
// это одна кнопка «BB↓»), нераспознанное/пустое — серый NR.
export function ratingColor(rating) {
  const b = ratingBucket(rating);
  if (b === "BB" || b === "B") return RT_COLOR.BELOW;
  return RT_COLOR[b] ?? RT_COLOR.NR;
}

// Семантика для сканируемости: DM выше/положительный = дёшево (up), ниже = дорого (down).
export function dmColor(dm) {
  if (dm == null) return { color: "var(--mut-2)" };
  return { color: dm >= 0 ? "var(--up)" : "var(--down)" };
}
