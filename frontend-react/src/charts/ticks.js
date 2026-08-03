// Тики оси времени. Раньше подписи брались фиксированным шагом
// (Math.floor(n / 4)) — на оси получались случайные даты «17.03 · 04.05 · 21.06».
// Здесь шаг выбирается по календарным границам (год / месяц / понедельник /
// день / час) в зависимости от длины окна, поэтому подписи «круглые».

const day = (t) => String(t).slice(0, 10);
const ms = (t) => Date.parse(day(t) + "T00:00:00Z");

// понедельник недели, к которой относится дата (ISO-строка) — ключ группировки
const weekKey = (t) => {
  const d = new Date(ms(t));
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
};

// ключ календарной группы по длине окна в днях
function keyFn(spanDays) {
  if (spanDays > 1100) return (t) => day(t).slice(0, 4);            // год
  if (spanDays > 150) return (t) => day(t).slice(0, 7);             // месяц
  if (spanDays > 25) return weekKey;                                // неделя
  if (spanDays > 2) return day;                                     // день
  return (t) => String(t).slice(0, 13);                             // час
}

// Индексы точек, попадающих на календарные границы. times — массив ISO-строк
// («2026-08-03» или «2026-08-03 10:35:00») по возрастанию. Возвращает не более
// target индексов; при нехватке границ — равномерный шаг (старое поведение).
export function dateTickIdx(times, target = 5) {
  const n = times.length;
  if (n === 0) return [];
  if (n <= target) return times.map((_, i) => i);

  const spanDays = Math.max((ms(times[n - 1]) - ms(times[0])) / 864e5, 0);
  const key = keyFn(spanDays);
  const firsts = [];
  let prev = null;
  for (let i = 0; i < n; i++) {
    const k = key(times[i]);
    if (k !== prev) { firsts.push(i); prev = k; }
  }
  if (firsts.length < 2) {
    const step = Math.max(1, Math.floor((n - 1) / (target - 1)));
    const out = [];
    for (let i = 0; i < n; i += step) out.push(i);
    return out;
  }
  // границ может быть много (52 недели) — прореживаем до target равномерно
  if (firsts.length <= target) return firsts;
  const stride = Math.ceil(firsts.length / target);
  return firsts.filter((_, i) => i % stride === 0);
}

// Подпись тика под длину окна: «03.08» / «авг 26» / «2026» / «10:35».
const MON = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
export function tickLabel(t, spanDays) {
  const d = day(t), [Y, M, D] = d.split("-");
  if (spanDays > 1100) return Y;
  if (spanDays > 150) return `${MON[+M - 1]} ${Y.slice(2)}`;
  if (spanDays > 2) return `${D}.${M}`;
  const hm = String(t).slice(11, 16);
  return hm || `${D}.${M}`;
}

// Длина окна в днях по краям массива ISO-времён (для tickLabel).
export function spanDays(times) {
  if (times.length < 2) return 0;
  return Math.max((ms(times[times.length - 1]) - ms(times[0])) / 864e5, 0);
}
