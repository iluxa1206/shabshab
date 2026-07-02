import { useEffect, useRef, useState } from "react";
import { COL_META, DEFAULT_COLS } from "./BondTable.jsx";

// Дропдаун выбора видимых столбцов. visibleCols — массив key; onToggle(key); onReset().
export default function ColumnsMenu({ visibleCols, onToggle, onReset }) {
  const [open, setOpen] = useState(false);
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

  return (
    <div className="colmenu" ref={ref}>
      <button className={"chip-btn" + (open ? " on" : "")} onClick={() => setOpen((v) => !v)}
        aria-haspopup="true" aria-expanded={open} title="Показать/скрыть столбцы">
        ⚙ СТОЛБЦЫ
      </button>
      {open && (
        <div className="colmenu-pop" role="menu">
          <div className="colmenu-head">
            <span>СТОЛБЦЫ</span>
            <button className="colmenu-reset" onClick={onReset}>сброс</button>
          </div>
          <div className="colmenu-list">
            {COL_META.map((c) => (
              <label key={c.key} className="colmenu-item">
                <input type="checkbox" checked={set.has(c.key)} onChange={() => onToggle(c.key)} />
                <span>{c.label}{c.sub ? <small> · {c.sub}</small> : null}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export { DEFAULT_COLS };
