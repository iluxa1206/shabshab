import { useEffect, useRef, useState } from "react";
import { ratingBucket, ratingColor } from "../format.js";

/**
 * «▾ все рейтинги» — дропдаун СТУПЕНЕЙ рядом с чипами грейдов.
 *
 * Чипы держат крупную шкалу (AAA / AA / A / BBB / BB↓ / NR) — она читается с
 * одного взгляда и не разъезжается, когда в справочниках появляются AA+/AA−.
 * Кому нужна ступень — берёт её здесь; выбор складывается в тот же массив, что
 * и чипы, поэтому «AA» (грейд, забирает всю группу) и «AA−» (только она)
 * работают вместе.
 *
 * options: [{name, count}] — ступени с числом бумаг; sel — общий массив выбора;
 * onToggle(name) — тот же обработчик, что у чипов.
 */
export default function RatingMenu({ options, sel, onToggle, onClear }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const items = options || [];
  if (!items.length) return null;
  // ступени, выбранные точечно (грейды живут в чипах и сюда не считаются)
  const names = new Set(items.map((o) => o.name));
  const picked = (sel || []).filter((k) => names.has(k) && k !== ratingBucket(k));

  return (
    <div className="rtmenu" ref={ref}>
      <button type="button" className={"chip-btn rtmenu-btn" + (open || picked.length ? " on" : "")}
        aria-haspopup="true" aria-expanded={open}
        title="Все рейтинги со ступенями (AA+, AA−, …). Чип грейда «AA» забирает всю группу; здесь можно выбрать конкретную ступень."
        onClick={() => setOpen((v) => !v)}>
        ▾{picked.length ? ` ${picked.length}` : ""}
      </button>
      {open && (
        <div className="rtmenu-pop" role="menu">
          <div className="rtmenu-head">
            <span>ВСЕ РЕЙТИНГИ</span>
            {onClear && picked.length > 0 && (
              <button type="button" className="colmenu-reset" onClick={onClear}>сброс</button>
            )}
          </div>
          <div className="rtmenu-list">
            {items.map((o) => {
              const on = (sel || []).includes(o.name);
              // ступень уже покрыта выбранным грейдом — показываем это, чтобы
              // не гадать, почему в ленте есть AA−, хотя выбран только «AA»
              const viaBucket = !on && (sel || []).includes(ratingBucket(o.name));
              return (
                <button type="button" key={o.name} role="menuitemcheckbox" aria-checked={on}
                  className={"rtmenu-item" + (on ? " on" : "") + (viaBucket ? " via" : "")}
                  onClick={() => onToggle(o.name)}
                  title={viaBucket ? `входит в выбранный грейд ${ratingBucket(o.name)}` : undefined}>
                  <span className="rtmenu-name" style={{ color: ratingColor(o.name) }}>{o.name}</span>
                  {o.count != null && <span className="rtmenu-cnt">{o.count}</span>}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
