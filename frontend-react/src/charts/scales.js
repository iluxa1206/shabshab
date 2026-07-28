// Геометрия самописных SVG-графиков (без chart-либ). Все шкалы — чистые функции
// домен→пиксели viewBox. Y-шкалы задают через инвертированный range ([низ, верх]).

// min/max массива значений + опциональный симметричный паддинг домена.
// pad — доля размаха; fallback — паддинг/размах при вырожденных данных.
export function extent(values, pad = 0, fallback = 1) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) {
    if (v == null || Number.isNaN(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!Number.isFinite(lo)) return [0, fallback];
  if (pad) { const p = (hi - lo) * pad || fallback; lo -= p; hi += p; }
  return [lo, hi];
}

// линейная шкала домен [d0,d1] → пиксели [r0,r1]. Для оси Y передавай range сверху вниз.
export function linearScale([d0, d1], [r0, r1]) {
  const span = (d1 - d0) || 1;
  const s = (v) => r0 + ((v - d0) / span) * (r1 - r0);
  s.invert = (px) => d0 + ((px - r0) / ((r1 - r0) || 1)) * span;
  return s;
}

// лог-шкала по x (короткий конец не слипается): d берётся не меньше 1.
export function logScale([d0, d1], [r0, r1]) {
  const l = (d) => Math.log(Math.max(d, 1));
  const a = l(d0), span = (l(d1) - a) || 1;
  return (v) => r0 + ((l(v) - a) / span) * (r1 - r0);
}

// sqrt-шкала по x: компромисс лин↔лог для term-structure. Короткий конец
// не слипается (как лог), но длинный не сплющивается (1Y ≈ 28% домена 10Y,
// против 63% у лога). Домен ≥ 0. Есть invert — для hover по позиции курсора.
export function sqrtScale([d0, d1], [r0, r1]) {
  const s0 = Math.sqrt(Math.max(d0, 0));
  const span = (Math.sqrt(Math.max(d1, 0)) - s0) || 1;
  const s = (v) => r0 + ((Math.sqrt(Math.max(v, 0)) - s0) / span) * (r1 - r0);
  s.invert = (px) => {
    const t = s0 + ((px - r0) / ((r1 - r0) || 1)) * span;
    return t * t;
  };
  return s;
}

// шкала времени: принимает Date | ms | ISO-строку.
export function timeScale([t0, t1], [r0, r1]) {
  const toMs = (x) => (x instanceof Date ? x.getTime() : typeof x === "number" ? x : new Date(x).getTime());
  const a = toMs(t0), span = (toMs(t1) - a) || 1;
  const s = (x) => r0 + ((toMs(x) - a) / span) * (r1 - r0);
  s.toMs = toMs;
  return s;
}

// count+1 равномерных значений на [d0,d1] (совместимо со старой сеткой).
export function linTicks(d0, d1, count) {
  const out = [];
  for (let i = 0; i <= count; i++) out.push(d0 + ((d1 - d0) * i) / count);
  return out;
}

// «красивые» тики (кратные 1/2/5·10^k) — примитив для новых графиков.
export function niceTicks(min, max, target = 5) {
  const span = (max - min) || 1;
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

// ms 1 января каждого года в диапазоне [t0,t1] (годовые тики оси времени).
export function yearTicks(t0, t1) {
  const out = [];
  const y0 = new Date(t0).getFullYear(), y1 = new Date(t1).getFullYear();
  for (let y = y0; y <= y1; y++) out.push(new Date(`${y}-01-01`).getTime());
  return out;
}
