import { RT_COLOR } from "../format.js";
import RatingMenu from "./RatingMenu.jsx";

// Чипы ГРЕЙДОВ рейтинга + меню ступеней «▾» — один фильтр на монитор, фиксы и
// первичку. Чипы держат крупную шкалу (AAA / AA / A / BBB / BB↓ / NR): она
// читается с одного взгляда и не разъезжается, когда в справочниках
// появляются AA+/AA−; ступени — в меню справа. Выбор складывается в один
// массив: грейд «AA» забирает всю группу, ступень «AA−» — только себя
// (правило совпадения — format.ratingMatches).
// Раньше блок жил тремя копиями (Toolbar, PrimaryCalendar, ...) и уже разошёлся.
export const RATINGS = [
  ["AAA", "AAA"], ["AA", "AA"], ["A", "A"], ["BBB", "BBB"], ["BELOW", "BB↓"], ["NR", "NR"],
];

export default function RatingChips({ sel, onToggle, options, title }) {
  const on = (v) => (sel || []).includes(v);
  return (
    <div className="fgroup" title={title}>
      {RATINGS.map(([v, l]) => (
        <button key={v} className={"chip-btn" + (on(v) ? " on" : "")} aria-pressed={on(v)}
          style={on(v)
            ? { background: RT_COLOR[v], borderColor: RT_COLOR[v], color: "var(--bg)" }
            : { color: RT_COLOR[v] }}
          onClick={() => onToggle(v)}>{l}</button>
      ))}
      <RatingMenu options={options} sel={sel} onToggle={onToggle} />
    </div>
  );
}
