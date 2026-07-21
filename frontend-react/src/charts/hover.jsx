import { useState } from "react";

// Единый hover: nearest-point по горизонтали. Работает и для точек, и для линий.
// px(point) → пиксель-X в координатах viewBox; viewW — ширина viewBox.
// Возвращает { hover, setHover, handlers } для навешивания на <svg>.
export function useNearestHover({ viewW, points, px }) {
  const [hover, setHover] = useState(null);
  const onMouseMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const mx = ((e.clientX - r.left) / r.width) * viewW;
    let best = null, bd = Infinity;
    for (const p of points) {
      const d = Math.abs(px(p) - mx);
      if (d < bd) { bd = d; best = p; }
    }
    setHover(best);
  };
  return { hover, setHover, handlers: { onMouseMove, onMouseLeave: () => setHover(null) } };
}

// Инверсная плашка-подсказка (--inv-bg/--inv-fg), позиционируется по X в % от viewBox.
// Родитель должен быть position:relative.
export function Tooltip({ x, viewW, top = 0, dy = -4, padding = "2px 6px", children }) {
  return (
    <div style={{
      position: "absolute", left: `${(x / viewW) * 100}%`, top,
      transform: `translate(-50%, ${dy}px)`, fontSize: 11,
      background: "var(--inv-bg)", color: "var(--inv-fg)", padding,
      borderRadius: 4, whiteSpace: "nowrap", pointerEvents: "none",
    }}>{children}</div>
  );
}
