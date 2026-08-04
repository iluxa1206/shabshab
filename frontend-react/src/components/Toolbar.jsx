import ColumnsMenu from "./ColumnsMenu.jsx";
import IssuerFilter from "./IssuerFilter.jsx";

const RATINGS = [
  ["AAA", "AAA"], ["AA", "AA"], ["A", "A"], ["BBB", "BBB"], ["BELOW", "BB↓"], ["NR", "NR"],
];
// цвета бакетов как в FixedModule (тема-aware CSS-переменные)
const RTCOLOR = {
  AAA: "var(--rt-aaa)", AA: "var(--rt-aa)", A: "var(--rt-a)", BBB: "var(--rt-bbb)",
  BELOW: "var(--rt-bb)", NR: "var(--mut-2)",
};

export default function Toolbar({
  onlyWatch, setOnlyWatch, basesSel, toggleBase, ratingsSel, toggleRating,
  issuers, emittersSel, toggleEmitter, clearEmitters, twoSided, setTwoSided,
  query, setQuery, watchCount, shown, total, showAnalytics, setShowAnalytics,
  visibleCols, onToggleCol, onResetCols,
}) {
  return (
    <section className="toolbar">
      <span className="search-wrap">
        <input
          className="search"
          type="text"
          placeholder="ISIN / имя"
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

      {/* группа 3: рейтинг — размер/форма как соседние chip-btn, цвет = бакет */}
      <div className="fgroup">
        {RATINGS.map(([v, l]) => (
          <button key={v} className={"chip-btn" + (ratingsSel.includes(v) ? " on" : "")}
            style={ratingsSel.includes(v)
              ? { background: RTCOLOR[v], borderColor: RTCOLOR[v], color: "var(--bg)" }
              : { color: RTCOLOR[v] }}
            onClick={() => toggleRating(v)}>{l}</button>
        ))}
      </div>

      {/* группа: ликвидность — только бумаги с обеими сторонами стакана */}
      <div className="fgroup">
        <button className={"chip-btn" + (twoSided ? " on" : "")} onClick={() => setTwoSided(!twoSided)}
          title="Показывать только бумаги с двусторонней котировкой (есть и бид, и оффер)">
          BID×OFFER
        </button>
      </div>

      {/* группа: эмитент */}
      <div className="fgroup">
        <IssuerFilter issuers={issuers || []} selected={emittersSel || []}
          onToggle={toggleEmitter} onClear={clearEmitters} />
      </div>

      <div className="fgroup">
        <button className={"chip-btn" + (showAnalytics ? " on" : "")}
          onClick={() => setShowAnalytics(!showAnalytics)} title="Кросс-секция рынка">
          📊 АНАЛИТИКА
        </button>
      </div>

      {/* группа: столбцы */}
      <div className="fgroup">
        <ColumnsMenu visibleCols={visibleCols} onToggle={onToggleCol} onReset={onResetCols} />
      </div>

      <span className="grow" />
      <span className="hint">{total ? `${shown} из ${total}` : "—"}</span>
    </section>
  );
}
