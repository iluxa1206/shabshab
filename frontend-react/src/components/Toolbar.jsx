import { useRef } from "react";
import ColumnsMenu from "./ColumnsMenu.jsx";

const RATINGS = [
  ["AAA", "AAA"], ["AA", "AA"], ["A", "A"], ["BBB", "BBB"], ["BELOW", "BB↓"], ["NR", "NR"],
];

export default function Toolbar({
  onlyWatch, setOnlyWatch, basesSel, toggleBase, ratingsSel, toggleRating,
  query, setQuery, watchCount, shown, total, showAnalytics, setShowAnalytics,
  onImportCsv, posCount, onClearPositions,
  visibleCols, onToggleCol, onResetCols,
}) {
  const fileRef = useRef(null);

  const onFile = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => onImportCsv(String(reader.result || ""));
    reader.readAsText(f);
    e.target.value = ""; // сброс — чтобы повторный выбор того же файла сработал
  };

  return (
    <section className="toolbar">
      <span className="search-wrap">
        <input
          className="search"
          type="text"
          placeholder="ISIN / NAME"
          aria-label="Поиск по рынку — ISIN или название"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && <button className="search-clear" aria-label="Очистить" onClick={() => setQuery("")}>×</button>}
      </span>

      {/* группа 1: watchlist */}
      <div className="fgroup">
        <button className={"chip-btn" + (onlyWatch ? " on" : "")} onClick={() => setOnlyWatch(!onlyWatch)}>
          ★ {watchCount}
        </button>
      </div>

      {/* группа 2: база */}
      <div className="fgroup">
        <button className={"chip-btn" + (basesSel.includes("KEYRATE") ? " on" : "")} onClick={() => toggleBase("KEYRATE")}>КС</button>
        <button className={"chip-btn" + (basesSel.includes("RUONIA") ? " on" : "")} onClick={() => toggleBase("RUONIA")}>RUONIA</button>
      </div>

      {/* группа 3: рейтинг */}
      <div className="fgroup">
        {RATINGS.map(([v, l]) => (
          <button key={v} className={"chip-btn" + (ratingsSel.includes(v) ? " on" : "")} onClick={() => toggleRating(v)}>{l}</button>
        ))}
      </div>

      <div className="fgroup">
        <button className={"chip-btn" + (showAnalytics ? " on" : "")}
          onClick={() => setShowAnalytics(!showAnalytics)} title="Кросс-секция рынка">
          📊 АНАЛИТИКА
        </button>
      </div>

      {/* группа: портфель — импорт CSV + столбцы */}
      <div className="fgroup">
        <input ref={fileRef} type="file" accept=".csv,text/csv,text/plain" hidden onChange={onFile} />
        <button className="chip-btn" onClick={() => fileRef.current?.click()}
          title="Импорт CSV: строки вида ISIN,количество">
          ⭱ ИМПОРТ CSV
        </button>
        {posCount > 0 && (
          <button className="chip-btn" onClick={onClearPositions} title="Очистить позиции портфеля">
            портфель {posCount} ×
          </button>
        )}
        <ColumnsMenu visibleCols={visibleCols} onToggle={onToggleCol} onReset={onResetCols} />
      </div>

      <span className="grow" />
      <span className="hint">{total ? `${shown} из ${total}` : "—"}</span>
    </section>
  );
}
