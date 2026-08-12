import ColumnsMenu from "./ColumnsMenu.jsx";
import FiltersMenu from "./FiltersMenu.jsx";
import { IconCoins, IconLink, IconSearch, IconTwoWay, IconUnlink, IconX } from "./icons.jsx";
import { RT_COLOR as RTCOLOR } from "../format.js";

const RATINGS = [
  ["AAA", "AAA"], ["AA", "AA"], ["A", "A"], ["BBB", "BBB"], ["BELOW", "BB↓"], ["NR", "NR"],
];

export default function Toolbar({
  onlyWatch, setOnlyWatch, basesSel, toggleBase, ratingsSel, toggleRating,
  clearBases, issuers, emittersSel, toggleEmitter, clearEmitters, twoSided, setTwoSided,
  hideSub, setHideSub, hideAmort, setHideAmort, clsSel, toggleCls,
  volBid, setVolBid, volAsk, setVolAsk, volMode, setVolMode,
  depthTs, depthLoading, matFrom, setMatFrom, matTo, setMatTo,
  spreadFrom, setSpreadFrom, spreadTo, setSpreadTo,
  query, setQuery, searchRef, watchCount, shown, total,
  visibleCols, onToggleCol, onResetCols, onMoveCol,
  activeFilters, onResetFilters,
}) {
  const volTitle = "Размер тикета по сторонам, млн ₽: заполненная сторона пересчитывается "
    + "в средневзвешенную цену набора этой суммы по лестнице стакана (деньги грязные: "
    + "кол-во × (номинал × цена% + НКД)), спред — к этой цене. Цепочка между полями: "
    + "целая — «И» (обе стороны должны набрать объём), разорванная — «ИЛИ» (достаточно одной). "
    + "Допуск 10%: сторона считается набравшей объём, если стакан даёт ≥90% запрошенного "
    + "(заявка «100 000 бумаг по 98» — это ~98 млн ₽ грязными, и по строгому порогу «100 млн» "
    + "она бы вылетела)."
    + (depthTs ? `\nСнимок стаканов: ${new Date(depthTs * 1000).toLocaleTimeString("ru-RU")}` : "");
  // млн (строка инпута) ↔ ₽ (состояние)
  const mlnToRub = (s) => {
    const v = parseFloat(s);
    return Number.isFinite(v) && v > 0 ? Math.round(v * 1e6) : 0;
  };
  return (
    <section className="toolbar">
      <span className="search-wrap">
        <IconSearch size={13} className="search-ico" />
        <input
          ref={searchRef}
          className="search"
          type="text"
          placeholder="ISIN / имя  ( / )"
          title={"Умный поиск: слова запроса ищутся по ISIN, имени, эмитенту и формуле в любом порядке "
            + "и с допуском опечатки. «РЖД 3» покажет все похожие выпуски (2Р3, 3Р2, 1Р-03R).\n"
            + "Хоткей: / — фокус, Esc — очистить"}
          aria-label="Поиск по рынку — ISIN или название"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            // Esc в поиске — очистить и уйти из поля (попапы Esc ловят у себя)
            if (e.key !== "Escape") return;
            e.stopPropagation();
            setQuery("");
            e.currentTarget.blur();
          }}
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
          hideSub={hideSub} setHideSub={setHideSub}
          hideAmort={hideAmort} setHideAmort={setHideAmort}
          clsSel={clsSel} toggleCls={toggleCls}
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
          aria-label="Только двусторонние котировки"
          title="BID×OFFER — показывать только бумаги с двусторонней котировкой (есть и бид, и оффер)">
          <IconTwoWay size={13} />
        </button>
      </div>

      {/* группа: объём тикета по сторонам — VWAP по стакану на эти суммы */}
      <div className="fgroup" title={volTitle}>
        <IconCoins size={13} className="fg-ico" />
        <input className="num-input" type="number" min="0" step="0.5" placeholder="bid"
          aria-label="Размер тикета на биде, млн ₽"
          value={volBid ? String(volBid / 1e6) : ""}
          onChange={(e) => setVolBid(mlnToRub(e.target.value))} />
        <button className={"chip-btn" + (volMode === "and" ? " on" : "")}
          aria-label="Связка условий bid/offer"
          title={volMode === "and"
            ? "И — обе стороны должны набрать свой объём (клик → ИЛИ)"
            : "ИЛИ — достаточно одной стороны (клик → И)"}
          onClick={() => setVolMode(volMode === "and" ? "or" : "and")}>
          {volMode === "and" ? <IconLink size={13} /> : <IconUnlink size={13} />}
        </button>
        <input className="num-input" type="number" min="0" step="0.5" placeholder="offer"
          aria-label="Размер тикета на оффере, млн ₽"
          value={volAsk ? String(volAsk / 1e6) : ""}
          onChange={(e) => setVolAsk(mlnToRub(e.target.value))} />
        {depthLoading && <span className="fg-lbl">…</span>}
      </div>

      {/* группа: окно погашения в годах */}
      <div className="fgroup" title="Лет до погашения в интервале [от, до]. Бумаги без даты погашения при заданной границе скрыты.">
        <span className="fg-lbl">MAT, Y</span>
        <input className="num-input" type="number" min="0" step="0.5" placeholder="от"
          aria-label="Лет до погашения — от" value={matFrom}
          onChange={(e) => setMatFrom(e.target.value)} />
        <span className="fg-lbl">—</span>
        <input className="num-input" type="number" min="0" step="0.5" placeholder="до"
          aria-label="Лет до погашения — до" value={matTo}
          onChange={(e) => setMatTo(e.target.value)} />
        {(matFrom || matTo) && (
          <button className="chip-btn" title="Сбросить окно погашения"
            onClick={() => { setMatFrom(""); setMatTo(""); }}>×</button>
        )}
      </div>

      {/* группа: окно спреда Y-IDX, bps */}
      <div className="fgroup" title="R-spread в интервале [от, до], bps. Границы применяются к тому же числу, что в колонке R-spread (с учётом фильтра по объёму). Бумаги без посчитанного спреда при заданной границе скрыты.">
        <span className="fg-lbl">R-spread</span>
        <input className="num-input" type="number" step="10" placeholder="от"
          aria-label="R-spread — от, bps" value={spreadFrom}
          onChange={(e) => setSpreadFrom(e.target.value)} />
        <span className="fg-lbl">—</span>
        <input className="num-input" type="number" step="10" placeholder="до"
          aria-label="R-spread — до, bps" value={spreadTo}
          onChange={(e) => setSpreadTo(e.target.value)} />
        {(spreadFrom || spreadTo) && (
          <button className="chip-btn" title="Сбросить окно спреда"
            onClick={() => { setSpreadFrom(""); setSpreadTo(""); }}>×</button>
        )}
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
