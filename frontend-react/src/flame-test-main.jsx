// Тест-харнесс «зелёного пламени» на имени свежего выпуска: настоящая ячейка
// INSTRUMENT из COLS (флоатеры) и FIXED_COLS (фиксы) без бэкенда и логина.
// Свежая бумага — размещение 6 дней назад, старая — весной. Переключатель темы
// внизу. Не входит в прод-бандл (отдельный entry).
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { COLS } from "./components/BondTable.jsx";
import { FIXED_COLS } from "./components/fixed/fixedCols.jsx";
import "./styles.css";

const ago = (d) => new Date(Date.now() - d * 864e5).toISOString().slice(0, 10);
const FLOAT = [
  { isin: "RU000A10CQ01", short_name: "Газпром нефть 003P-15R", issue_date: ago(180), rating: "AAA" },
  { isin: "RU000A10DK47", short_name: "Атомэнергопром 001P-07", issue_date: ago(6), rating: "AAA" },
  { isin: "RU000A10DN52", short_name: "Самолёт БО-П18", issue_date: ago(13), rating: "A", price_thin: true },
  { isin: "SU29026RMFS3", short_name: "ОФЗ 29026", issue_date: ago(400) },
];
const FIXED = [
  { isin: "RU000A10BX23", name: "РЖД 001P-45R", issue_date: ago(120), cls: "corp", rating: "AAA" },
  { isin: "RU000A10DQ19", name: "МТС 002P-12", issue_date: ago(2), cls: "corp", rating: "AAA" },
  { isin: "SU26248RMFS3", name: "ОФЗ 26248", issue_date: ago(3), cls: "ofz" },
];
const nameCol = (cols, key) => cols.find((c) => c.key === key);

function App() {
  const [theme, setTheme] = useState("theme-dark");
  const fc = nameCol(COLS, "short_name"), xc = nameCol(FIXED_COLS, "name");
  return (
    <div id="app" className={theme} style={{ padding: 24, gap: 24 }}>
      <div style={{ display: "flex", gap: 8 }}>
        {["theme-light", "theme-dark", "theme-win"].map((t) => (
          <button key={t} className="btn" onClick={() => setTheme(t)}>{t}</button>
        ))}
      </div>
      <h3 style={{ margin: 0 }}>Флоатеры (COLS)</h3>
      <table className="grid"><tbody>{FLOAT.map((b) => <tr key={b.isin}>{fc.cell(b)}</tr>)}</tbody></table>
      <h3 style={{ margin: 0 }}>Фиксы (FIXED_COLS)</h3>
      <table className="grid"><tbody>{FIXED.map((b) => <tr key={b.isin}>{xc.cell(b)}</tr>)}</tbody></table>
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
