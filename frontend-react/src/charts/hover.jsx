import { useState, useRef, useLayoutEffect } from "react";

// Единый hover: nearest-point по горизонтали. Работает и для точек, и для линий.
// px(point) → пиксель-X в координатах viewBox; viewW — ширина viewBox.
// onHover (опц.) — колбэк наружу (синхронизация курсора между графиками).
// Возвращает { hover, setHover, handlers } для навешивания на <svg>.
//
// Тач: pointer-события покрывают мышь, перо и палец одним кодом (раньше был
// только onMouseMove — на планшете графики были мертвы). touch-action: none
// на .an-svg в CSS, иначе браузер съедает движение пальца под скролл.
export function useNearestHover({ viewW, points, px, onHover }) {
  const [hover, setHover] = useState(null);

  const pick = (clientX, target) => {
    const r = target.getBoundingClientRect();
    const mx = ((clientX - r.left) / r.width) * viewW;
    let best = null, bd = Infinity;
    for (const p of points) {
      const d = Math.abs(px(p) - mx);
      if (d < bd) { bd = d; best = p; }
    }
    setHover(best);
    onHover?.(best ?? null);
  };
  const clear = () => { setHover(null); onHover?.(null); };

  return {
    hover,
    setHover,
    handlers: {
      onPointerMove: (e) => pick(e.clientX, e.currentTarget),
      onPointerDown: (e) => pick(e.clientX, e.currentTarget),
      onPointerLeave: clear,
      // палец убран с экрана — курсор снимаем (pointerleave для touch не летит)
      onPointerUp: (e) => { if (e.pointerType !== "mouse") clear(); },
      onPointerCancel: clear,
    },
  };
}

// Ховер дискретных элементов (точки scatter, строки box-графика). Заменяет
// нативный <title>: тот появляется через ~1.5 c, не стилизуется и не работает
// на тач. Возвращает bind(x, y, content) — набор pointer-обработчиков.
export function useElementHover() {
  const [hover, setHover] = useState(null); // { x, y, content }
  const bind = (x, y, content) => ({
    onPointerEnter: () => setHover({ x, y, content }),
    onPointerLeave: () => setHover(null),
  });
  return { hover, bind, clear: () => setHover(null) };
}

// Инверсная плашка-подсказка (--inv-bg/--inv-fg). Позиционируется либо по X
// в % от viewBox (передан viewW), либо в абсолютных px (viewW не передан).
// y (опц.) — вертикальная привязка в px вместо top. Родитель — position:relative.
//
// Раньше край ловился тремя дискретными сдвигами якоря (0/-50/-100%): широкая
// плашка всё равно вылезала за карточку и обрезалась. Теперь замеряем её
// реальную ширину и зажимаем left в пределах контейнера; maxWidth не даёт
// плашке быть шире графика (длинный текст переносится).
const TT_MARGIN = 4;

export function Tooltip({ x, y, viewW, top = 0, dy = -4, padding = "2px 6px", multiline = false, children }) {
  const ref = useRef(null);
  const [box, setBox] = useState(null); // { left, maxW }

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const host = el.offsetParent || el.parentElement;
    const cw = host ? host.clientWidth : 0;
    if (!cw) return;
    const maxW = Math.max(60, cw - 2 * TT_MARGIN);
    const w = Math.min(el.offsetWidth, maxW);
    const anchor = viewW ? (x / viewW) * cw : x;
    const left = Math.max(TT_MARGIN, Math.min(anchor - w / 2, cw - w - TT_MARGIN));
    if (!box || Math.abs(box.left - left) > 0.5 || box.maxW !== maxW) setBox({ left, maxW });
  });

  return (
    <div ref={ref} style={{
      position: "absolute",
      left: box ? box.left : 0,
      maxWidth: box ? box.maxW : undefined,
      top: y == null ? top : y,
      transform: `translate(0, ${y == null ? dy : dy - 10}px)`, fontSize: 11,
      visibility: box ? "visible" : "hidden",
      background: "var(--inv-bg)", color: "var(--inv-fg)", padding,
      borderRadius: 4,
      whiteSpace: multiline ? "pre-line" : "normal",
      overflowWrap: "anywhere",
      pointerEvents: "none", zIndex: 2,
      lineHeight: 1.35, boxShadow: "0 2px 8px rgba(0,0,0,.18)",
    }}>{children}</div>
  );
}
