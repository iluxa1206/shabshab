// SVG path-билдеры для линий/ступеней/стек-баров. fx/fy — акцессоры точки → пиксель.

const P = (x, y) => `${x.toFixed(1)},${y.toFixed(1)}`;

// гладкая полилиния M…L…
export function linePath(items, fx, fy) {
  return items.map((d, i) => `${i ? "L" : "M"}${P(fx(d, i), fy(d, i))}`).join(" ");
}

// ступенчатый путь (step-after): горизонт на уровне предыдущей точки, затем вертикаль.
export function stepPath(items, fx, fy) {
  return items.map((d, i) => {
    const x = fx(d, i), y = fy(d, i);
    if (i === 0) return `M${P(x, y)}`;
    return `L${P(x, fy(items[i - 1], i - 1))} L${P(x, y)}`;
  }).join(" ");
}

// стек-бар снизу вверх. segments: [{ value, ...meta }]; sy(value) → высота.
// baseY — низ бара (пиксель). Возвращает сегменты с { y, h } (пропускает тонкие <0.5px).
export function stackedBars(segments, baseY, sy) {
  let y = baseY;
  const out = [];
  for (const seg of segments) {
    const h = sy(seg.value);
    y -= h;
    if (h > 0.5) out.push({ ...seg, y, h });
  }
  return out;
}
