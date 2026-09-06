import { ICOLORS, OTHER_COLOR, issuerColors } from "../issuerPalette.js";

const trunc = (s, n = 16) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

/** Легенда раскраски по эмитентам: те же имена, что получили цвет (порядок
 *  совпадает с issuerColors), плюс «прочие» серым. */
export default function IssuerLegend({ rows, keyFn }) {
  const icol = issuerColors(rows, keyFn);
  if (!icol.size) return null;
  const rest = new Set(rows.map(keyFn).filter(Boolean)).size - icol.size;
  return (
    <div className="an-legend">
      {[...icol.entries()].map(([k, c]) => (
        <span key={k} className="an-leg-item">
          <span className="an-leg-swatch" style={{ background: c }} />{trunc(String(k))}
        </span>
      ))}
      {rest > 0 && (
        <span className="an-leg-item">
          <span className="an-leg-swatch" style={{ background: OTHER_COLOR }} />прочие ({rest})
        </span>
      )}
    </div>
  );
}

export { ICOLORS, OTHER_COLOR, issuerColors };
