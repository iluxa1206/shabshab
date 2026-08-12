// Статус активной вкладки в общей нижней полосе (StatusBar).
//
// Раньше каждая вкладка рисовала свои итоги отдельной строкой над статус-баром:
// две полосы подряд, каждая со своими отступами и границей. Полоса в
// приложении должна быть одна — вкладка публикует сюда пары «подпись:значение»,
// StatusBar их показывает рядом с темой, датами и часами.
//
// Держим ПЛОСКИЕ данные, а не готовый JSX: полосе решать, как их верстать, а
// сравнение по значению избавляет от лишних перерисовок.
import { createContext, useContext, useEffect, useMemo, useState } from "react";

const Ctx = createContext(null);

export function PageStatusProvider({ children }) {
  const [items, setItems] = useState([]);
  const value = useMemo(() => ({ items, setItems }), [items]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Опубликовать итоги вкладки. items: [{k, v, cls?, title?}] — пустое значение
 *  или null в списке отбрасывается. При уходе со страницы полоса очищается. */
export function usePageStatus(items) {
  const ctx = useContext(Ctx);
  const clean = (items || []).filter(Boolean).filter((i) => i.v != null && i.v !== "");
  const key = JSON.stringify(clean);
  const set = ctx?.setItems;
  useEffect(() => {
    if (!set) return undefined;
    set(clean);
    return () => set([]);
    // сравнение по СОДЕРЖИМОМУ: массив пересобирается каждый рендер, и по
    // ссылке это был бы бесконечный цикл setState → render → setState
  }, [key, set]);   // eslint-disable-line react-hooks/exhaustive-deps
}

export function usePageStatusItems() {
  return useContext(Ctx)?.items || [];
}
