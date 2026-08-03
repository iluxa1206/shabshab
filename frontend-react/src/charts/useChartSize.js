import { useCallback, useLayoutEffect, useRef, useState } from "react";

// Реальный размер контейнера графика (ResizeObserver).
//
// Зачем: раньше каждый график рисовался в фиксированный viewBox (460×220) и
// растягивался CSS'ом на ширину панели. Вместе с геометрией растягивался и
// текст: .an-axis 9.5px в узкой панели превращался в ~6px, в широком окне —
// в ~14px, штрихи сетки толстели. Теперь viewBox = измеренные пиксели 1:1,
// масштабирования нет, шрифт осей постоянный на любой ширине.
//
// height — фиксированная высота; aspect (опц.) — высота как доля ширины
// (приоритетнее height). Возвращает ref на контейнер и размеры для <svg>.
export function useChartSize({ height = 200, aspect, minWidth = 260, maxHeight } = {}) {
  const [w, setW] = useState(0);
  const nodeRef = useRef(null);
  const ref = useCallback((node) => { nodeRef.current = node; }, []);

  useLayoutEffect(() => {
    const node = nodeRef.current;
    if (!node) return;
    // сразу после монтирования RO ещё не сработал — снимаем размер вручную,
    // иначе первый кадр рисуется по minWidth и «прыгает»
    setW(Math.round(node.getBoundingClientRect().width));
    const ro = new ResizeObserver((entries) => {
      const next = Math.round(entries[0].contentRect.width);
      setW((prev) => (prev === next ? prev : next));
    });
    ro.observe(node);
    return () => ro.disconnect();
  }, []);

  const width = Math.max(w, minWidth);
  let h = aspect ? Math.round(width * aspect) : height;
  if (maxHeight) h = Math.min(h, maxHeight);
  return { ref, width, height: h, measured: w > 0 };
}
