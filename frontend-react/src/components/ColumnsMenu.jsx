import { useEffect, useRef, useState } from "react";
import { COL_META, DEFAULT_COLS } from "./BondTable.jsx";
import { IconColumns } from "./icons.jsx";

// Дропдаун столбцов: видимость (чекбокс) + ПОРЯДОК (перетаскивание пункта или
// стрелки ↑/↓). visibleCols — массив key В ПОРЯДКЕ ОТОБРАЖЕНИЯ; onToggle(key);
// onMove(key, target|"+1"|"-1"); onReset().
export default function ColumnsMenu({ visibleCols, onToggle, onReset, onMove }) {
  const [open, setOpen] = useState(false);
  const [dragKey, setDragKey] = useState(null);   // только для подсветки
  const dragRef = useRef(null);                   // источник правды в обработчиках
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onEsc); };
  }, [open]);

  const set = new Set(visibleCols);
  // сначала видимые в пользовательском порядке, следом скрытые (в порядке COLS)
  const meta = new Map(COL_META.map((c) => [c.key, c]));
  const items = [
    ...visibleCols.map((k) => meta.get(k)).filter(Boolean),
    ...COL_META.filter((c) => !set.has(c.key)),
  ];

  return (
    <div className="colmenu" ref={ref}>
      <button className={"chip-btn" + (open ? " on" : "")} onClick={() => setOpen((v) => !v)}
        aria-haspopup="true" aria-expanded={open} aria-label="Столбцы"
        title="Показать/скрыть столбцы">
        <IconColumns size={13} />
      </button>
      {open && (
        <div className="colmenu-pop" role="menu">
          <div className="colmenu-head">
            <span>СТОЛБЦЫ</span>
            <button className="colmenu-reset" onClick={onReset}>сброс</button>
          </div>
          <div className="colmenu-hint">перетащи за ⠿ или жми ↑↓, чтобы поменять порядок</div>
          <div className="colmenu-list">
            {items.map((c) => {
              const on = set.has(c.key);
              return (
                <div key={c.key}
                  className={"colmenu-item" + (on ? "" : " off") + (dragKey === c.key ? " dragging" : "")}
                  draggable={on && !!onMove}
                  onDragStart={(e) => {
                    dragRef.current = c.key; setDragKey(c.key);
                    e.dataTransfer.effectAllowed = "move";
                    try { e.dataTransfer.setData("text/plain", c.key); } catch { /* Safari */ }
                  }}
                  onDragEnd={() => { dragRef.current = null; setDragKey(null); }}
                  onDragOver={(e) => { if (dragRef.current && on) e.preventDefault(); }}
                  onDrop={(e) => {
                    e.preventDefault();
                    const from = dragRef.current || e.dataTransfer.getData("text/plain");
                    if (from && from !== c.key && on) onMove(from, c.key);
                    dragRef.current = null; setDragKey(null);
                  }}>
                  <span className="colmenu-grip" aria-hidden="true">{on && onMove ? "⠿" : ""}</span>
                  <label className="colmenu-label">
                    <input type="checkbox" checked={on} onChange={() => onToggle(c.key)} />
                    <span>{c.label}{c.sub ? <small> · {c.sub}</small> : null}</span>
                  </label>
                  {on && onMove && (
                    <span className="colmenu-move">
                      <button aria-label="Выше" title="Левее в таблице" onClick={() => onMove(c.key, "-1")}>↑</button>
                      <button aria-label="Ниже" title="Правее в таблице" onClick={() => onMove(c.key, "+1")}>↓</button>
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export { DEFAULT_COLS };
