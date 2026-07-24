import { useEffect, useMemo, useRef, useState } from "react";

// Мульти-селект эмитентов: кнопка «Эмитент (N)» → поисковый чеклист.
// issuers: [{name, count}] (уже отсортированы по count desc). selected: string[].
export default function IssuerFilter({ issuers, selected, onToggle, onClear }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onEsc); };
  }, [open]);

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    const list = s ? issuers.filter((i) => i.name.toLowerCase().includes(s)) : issuers;
    return list.slice(0, 200);
  }, [issuers, q]);

  const n = selected.length;
  return (
    <div className="issuer-filter" ref={ref}>
      <button className={"chip-btn" + (n ? " on" : "")} onClick={() => setOpen((o) => !o)}
        title="Фильтр по эмитенту">
        ЭМИТЕНТ{n ? ` (${n})` : ""} ▾
      </button>
      {open && (
        <div className="issuer-pop">
          <input className="issuer-search" placeholder="поиск эмитента…" autoFocus
            value={q} onChange={(e) => setQ(e.target.value)} />
          {n > 0 && <button className="issuer-clear" onClick={onClear}>сбросить {n}</button>}
          <div className="issuer-list">
            {shown.length === 0 && <div className="issuer-empty">нет совпадений</div>}
            {shown.map((i) => (
              <label key={i.name} className="issuer-row">
                <input type="checkbox" checked={selected.includes(i.name)}
                  onChange={() => onToggle(i.name)} />
                <span className="issuer-name">{i.name}</span>
                <span className="issuer-cnt">{i.count}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
