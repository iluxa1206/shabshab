import { useEffect, useMemo, useRef, useState } from "react";

// Кнопка «ФИЛЬТРЫ» → всплывающее окно: база (КС/RUONIA) и эмитент.
// Счётчик на кнопке — сколько фильтров активно ВСЕГО (не только тех, что внутри окна),
// в пару к кнопке сброса в тулбаре.
export default function FiltersMenu({
  basesSel, toggleBase, clearBases,
  issuers, emittersSel, toggleEmitter, clearEmitters,
  activeCount,
}) {
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

  const nEm = emittersSel.length;
  return (
    <div className="filters-menu" ref={ref}>
      <button className={"chip-btn" + (activeCount ? " on" : "")} onClick={() => setOpen((o) => !o)}
        title="Фильтры: база, эмитент, сброс">
        ФИЛЬТРЫ{activeCount ? ` (${activeCount})` : ""} ▾
      </button>
      {open && (
        <div className="filters-pop">
          <div className="fp-sec">
            <div className="fp-head">
              <span className="fg-lbl">БАЗА</span>
              {basesSel.length > 0 && (
                <button className="issuer-clear" onClick={clearBases}>сбросить</button>
              )}
            </div>
            <div className="fp-chips">
              <button className={"chip-btn" + (basesSel.includes("KEYRATE") ? " on" : "")}
                onClick={() => toggleBase("KEYRATE")}>КС</button>
              <button className={"chip-btn" + (basesSel.includes("RUONIA") ? " on" : "")}
                onClick={() => toggleBase("RUONIA")}>RUONIA</button>
            </div>
          </div>

          <div className="fp-sec">
            <div className="fp-head">
              <span className="fg-lbl">ЭМИТЕНТ{nEm ? ` (${nEm})` : ""}</span>
              {nEm > 0 && <button className="issuer-clear" onClick={clearEmitters}>сбросить {nEm}</button>}
            </div>
            <input className="issuer-search" placeholder="поиск эмитента…"
              value={q} onChange={(e) => setQ(e.target.value)} />
            <div className="issuer-list">
              {shown.length === 0 && <div className="issuer-empty">нет совпадений</div>}
              {shown.map((i) => (
                <label key={i.name} className="issuer-row">
                  <input type="checkbox" checked={emittersSel.includes(i.name)}
                    onChange={() => toggleEmitter(i.name)} />
                  <span className="issuer-name">{i.name}</span>
                  <span className="issuer-cnt">{i.count}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
