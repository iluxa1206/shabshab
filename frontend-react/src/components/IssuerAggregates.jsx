import { useMemo, useState } from "react";
import { fmt, dmColor } from "../format.js";

const RATINGS = [["AAA", "AAA"], ["AA", "AA"], ["A", "A"], ["BBB", "BBB"], ["BELOW", "BB↓"], ["NR", "NR"]];
const RORDER = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D"];
const ratingMatch = (r, sel) => sel.some((k) =>
  k === "NR" ? !r : k === "BELOW" ? (r && RORDER.indexOf(r) > RORDER.indexOf("BBB")) : k === r);

const median = (a) => {
  const s = a.filter((x) => x != null).sort((x, y) => x - y);
  if (!s.length) return null;
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const avg = (a) => {
  const s = a.filter((x) => x != null);
  return s.length ? s.reduce((p, c) => p + c, 0) / s.length : null;
};

// Агрегаты параметров по эмитентам: справедливые уровни DM/z/Y−IDX по эмитенту.
// Группируем по emitter_name; implausible-цены уже занулены на бэке (не искажают).
export default function IssuerAggregates({ bonds, onPickIssuer }) {
  const [sort, setSort] = useState({ key: "n", dir: "desc" });
  const [ratingsSel, setRatingsSel] = useState([]);
  const toggleRating = (v) =>
    setRatingsSel((a) => (a.includes(v) ? a.filter((x) => x !== v) : [...a, v]));

  const rows = useMemo(() => {
    const g = new Map();
    for (const b of bonds) {
      const k = b.emitter_name;
      if (!k) continue;
      if (b.price_implausible || b.price_thin) continue;   // стейл/тонкие цены — вне медиан
      if (ratingsSel.length && !ratingMatch(b.rating, ratingsSel)) continue;   // фильтр по рейтингу
      if (!g.has(k)) g.set(k, []);
      g.get(k).push(b);
    }
    const out = [];
    for (const [name, arr] of g.entries()) {
      out.push({
        name, n: arr.length,
        dm: median(arr.map((b) => b.disc_margin_bps)),
        sm: median(arr.map((b) => b.dm_bps)),
        z: median(arr.map((b) => b.z_model_bps)),
        yoi: median(arr.map((b) => b.yield_over_index_bps)),
        spread: avg(arr.map((b) => b.spread_issue_bps)),
        rating: arr.map((b) => b.rating).find(Boolean) || "—",
      });
    }
    const { key, dir } = sort;
    const m = dir === "asc" ? 1 : -1;
    out.sort((a, b) => {
      let x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return typeof x === "string" ? x.localeCompare(y) * m : (x - y) * m;
    });
    return out;
  }, [bonds, sort, ratingsSel]);

  const onSort = (key) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));

  const COLS = [
    ["name", "ЭМИТЕНТ", "left"], ["rating", "RATING", "left"], ["n", "БУМАГ", "num"],
    ["dm", "MED DM", "num"], ["sm", "MED SM", "num"], ["z", "MED Z", "num"],
    ["yoi", "MED Y−IDX", "num"], ["spread", "AVG SPREAD", "num"],
  ];
  const cell = (r, key) => {
    if (key === "name") return r.name;
    if (key === "rating") return r.rating;
    if (key === "n") return r.n;
    const v = r[key];
    if (v == null) return "—";
    return <span style={dmColor(v)}>{fmt.bps(Math.round(v))}</span>;
  };

  return (
    <div className="issuer-agg">
      <div className="ia-head">
        <h2 className="ia-title">Агрегаты по эмитентам</h2>
        <span className="ia-hint">медианы DM/z/Y−IDX по бумагам эмитента — справедливые уровни. Клик по строке → фильтр в «Флоатеры». Стейл-цены исключены.</span>
        <div className="ia-filters">
          <span className="ia-flabel">рейтинг:</span>
          {RATINGS.map(([v, l]) => (
            <button key={v} className={"chip-btn" + (ratingsSel.includes(v) ? " on" : "")}
              onClick={() => toggleRating(v)}>{l}</button>
          ))}
          {ratingsSel.length > 0 && (
            <button className="chip-btn" onClick={() => setRatingsSel([])}>сброс ×</button>
          )}
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="ia-empty">эмитенты подгружаются (бэкфилл MOEX EMITTER_ID, ~40/цикл) — обнови позже</div>
      ) : (
        <div className="ia-table-wrap">
          <table className="grid ia-table packed">
            <thead>
              <tr>
                {COLS.map(([k, lbl, al]) => (
                  <th key={k} className={al === "num" ? "num" : "left"}
                    onClick={() => onSort(k)} style={{ cursor: "pointer" }}>
                    {lbl}{sort.key === k ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                ))}
                <th className="fill-col" aria-hidden="true" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name} className="ia-row" onClick={() => onPickIssuer?.(r.name)} style={{ cursor: "pointer" }}>
                  {COLS.map(([k, , al]) => (
                    <td key={k} className={al === "num" ? "num" : "left"}>{cell(r, k)}</td>
                  ))}
                  <td className="fill-col" />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
