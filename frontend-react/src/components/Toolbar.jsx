import ColumnsMenu from "./ColumnsMenu.jsx";
import FiltersMenu from "./FiltersMenu.jsx";
import { IconChart, IconSearch, IconX } from "./icons.jsx";
import { RT_COLOR as RTCOLOR } from "../format.js";

const RATINGS = [
  ["AAA", "AAA"], ["AA", "AA"], ["A", "A"], ["BBB", "BBB"], ["BELOW", "BB↓"], ["NR", "NR"],
];

// пресеты размера тикета, ₽ (подписи в млн — как их называет трейдер)
const VOLS = [[1e6, "1"], [5e6, "5"], [10e6, "10"]];

export default function Toolbar({
  onlyWatch, setOnlyWatch, basesSel, toggleBase, ratingsSel, toggleRating,
  clearBases, issuers, emittersSel, toggleEmitter, clearEmitters, twoSided, setTwoSided,
  volRub, setVolRub, depthTs, depthLoading, matFrom, setMatFrom, matTo, setMatTo,
  query, setQuery, watchCount, shown, total, showAnalytics, setShowAnalytics,
  visibleCols, onToggleCol, onResetCols, onMoveCol,
  activeFilters, onResetFilters,
}) {
  const volTitle = "Размер тикета: BID/OFFER пересчитываются в средневзвешенную цену "
    + "набора этой суммы по лестнице стакана (деньги грязные: кол-во × (номинал × цена% + НКД)), "
    + "спред — к этой цене. Бумаги, где столько не набирается ни на одной стороне, скрыты."
    + (depthTs ? `\nСнимок стаканов: ${new Date(depthTs * 1000).toLocaleTimeString("ru-RU")}` : "");
  return (
    <section className="toolbar">
      <span className="search-wrap">
        <IconSearch size={13} className="search-ico" />
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

      {/* группа 2: фильтры (база, эмитент, сброс всего) */}
      <div className="fgroup">
        <FiltersMenu
          basesSel={basesSel} toggleBase={toggleBase} clearBases={clearBases}
          issuers={issuers || []} emittersSel={emittersSel || []}
          toggleEmitter={toggleEmitter} clearEmitters={clearEmitters}
          activeCount={activeFilters}
        />
        <button className="chip-btn reset-btn" disabled={!activeFilters} onClick={onResetFilters}
          aria-label="Сбросить все фильтры"
          title="Снять все фильтры: watchlist, база, рейтинг, эмитент, BID×OFFER, объём, погашение, поиск">
          <IconX size={12} />
        </button>
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

      {/* группа: объём тикета — VWAP по стакану на эту сумму */}
      <div className="fgroup" title={volTitle}>
        <span className="fg-lbl">ОБЪЁМ</span>
        {VOLS.map(([v, l]) => (
          <button key={v} className={"chip-btn" + (volRub === v ? " on" : "")}
            onClick={() => setVolRub(volRub === v ? 0 : v)}>{l}М</button>
        ))}
        <input className="num-input" type="number" min="0" step="0.5" placeholder="млн"
          aria-label="Размер тикета, млн ₽"
          value={volRub ? String(volRub / 1e6) : ""}
          onChange={(e) => {
            const v = parseFloat(e.target.value);
            setVolRub(Number.isFinite(v) && v > 0 ? Math.round(v * 1e6) : 0);
          }} />
        {depthLoading && <span className="fg-lbl">…</span>}
      </div>

      {/* группа: окно погашения */}
      <div className="fgroup" title="Погашение в интервале [от, до]. Бумаги без даты погашения при заданной границе скрыты.">
        <span className="fg-lbl">MAT</span>
        <input className="date-input" type="date" value={matFrom} aria-label="Погашение от"
          onChange={(e) => setMatFrom(e.target.value)} />
        <span className="fg-lbl">—</span>
        <input className="date-input" type="date" value={matTo} aria-label="Погашение до"
          onChange={(e) => setMatTo(e.target.value)} />
        {(matFrom || matTo) && (
          <button className="chip-btn" title="Сбросить окно погашения"
            onClick={() => { setMatFrom(""); setMatTo(""); }}>×</button>
        )}
      </div>

      <div className="fgroup">
        <button className={"chip-btn" + (showAnalytics ? " on" : "")}
          onClick={() => setShowAnalytics(!showAnalytics)}
          aria-label="Аналитика" title="Аналитика — кросс-секция рынка">
          <IconChart size={13} />
        </button>
      </div>

      {/* группа: столбцы */}
      <div className="fgroup">
        <ColumnsMenu visibleCols={visibleCols} onToggle={onToggleCol} onReset={onResetCols} onMove={onMoveCol} />
      </div>

      <span className="grow" />
      <span className="hint">{total ? `${shown} из ${total}` : "—"}</span>
    </section>
  );
}
