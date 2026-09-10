// Общее для scatter'ов «спред vs СРОК» (флоатеры и фиксы): тики оси срока и
// раскладка подписей точек. Копий быть не должно — иначе шаг сетки на двух
// вкладках разъедется.

// Тики оси СРОК стоят на календарных шагах — месяц/квартал/полгода/год/…, — а
// не на «красивых» числах niceTicks: подпись месяцами (fmt.yrs) округляет, и
// шаг 0,07 г давал ряд «1м 1м 2м 2м» с дублями. Берём самый мелкий шаг, при
// котором тиков не больше, чем влезает по ширине.
const TERM_STEPS = [1 / 12, 0.25, 0.5, 1, 2, 5, 10];

export function termTicks(min, max, maxN) {
  // шаг крупнее десяти лет не заводим (перпы и тридцатилетки — редкость, а
  // «25 лет» на оси читается хуже, чем «20 · 30»): если и с ним тиков больше
  // лимита, прореживаем готовый ряд
  const step = TERM_STEPS.find((st) => (max - min) / st <= maxN) ?? TERM_STEPS[TERM_STEPS.length - 1];
  const out = [];
  for (let k = Math.ceil(min / step - 1e-9); k * step <= max + 1e-9; k++) out.push(k * step);
  const every = Math.ceil(out.length / Math.max(1, maxN));
  return every > 1 ? out.filter((_, i) => i % every === 0) : out;
}

// Подписи точек: «имя 470» справа от кружка. Пропускаем те, что налезли бы на
// уже поставленную: при сотнях выпусков подписать все нельзя, а каша из
// наложенного текста хуже, чем её отсутствие.
// text (опц.) — своя подпись точки: у scatter'а «YTM vs дюрация» интересна не
// сама Y-координата (доходности ОФЗ отличаются в третьем знаке), а отклонение
// от КБД. Без параметра — прежнее «имя + округлённый Y».
export function placeLabels(pts, sx, sy, W, right, fs = 11, text = null) {
  const put = [], box = [], h = fs + 3;
  for (const p of pts.slice().sort((a, b) => b.y - a.y)) {
    const nm = String(p.name || p.isin || "");
    const short = nm.length > 18 ? nm.slice(0, 17) + "…" : nm;
    const txt = text ? text(p, short) : `${short} ${Math.round(p.y)}`;
    const w = txt.length * fs * 0.56;
    const x = sx(p.x) + 8, y = sy(p.y) + fs * 0.35;
    if (x + w > W - right) continue;                 // за правым полем не рисуем
    const r = { x1: x, y1: y - h, x2: x + w, y2: y };
    if (box.some((q) => r.x1 < q.x2 && r.x2 > q.x1 && r.y1 < q.y2 && r.y2 > q.y1)) continue;
    box.push(r);
    put.push({ key: p.isin || p.name, x, y, txt });
  }
  return put;
}
