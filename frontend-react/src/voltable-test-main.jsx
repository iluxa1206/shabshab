// Тест-харнесс фильтров ОБЪЁМ (VWAP по стакану) и ПОГАШЕНИЕ: тулбар + таблица
// на моковых строках и моковых лестницах, без бэкенда и логина. Считает та же
// applyVolume, что в App. Не входит в прод-бандл (отдельный entry).
import { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Toolbar from "./components/Toolbar.jsx";
import BondTable, { DEFAULT_COLS } from "./components/BondTable.jsx";
import { applyVolume } from "./vwap.js";
import "./styles.css";

// алертов в харнессе нет — глушим запрос таблицы, чтобы не сыпал 401
window.fetch = async (url) =>
  new Response(JSON.stringify(String(url).includes("/alerts") ? [] : {}),
               { headers: { "Content-Type": "application/json" } });

const row = (isin, name, mat, bid, ask, yBid, yAsk) => ({
  isin, short_name: name, base_rate_type: "KEYRATE", formula: "Ключевая ставка + 2%",
  spread_issue_bps: 200, coupons_per_year: 12, maturity_date: mat,
  next_coupon_date: "2026-09-01", last_price_pct: (bid + ask) / 2,
  bid_price_pct: bid, ask_price_pct: ask, y_idx_bid_bps: yBid, y_idx_ask_bps: yAsk,
  dirty_price_rub: 1012.5, dm_bps: 190, disc_margin_bps: 185, z_model_bps: 210,
  yield_xirr_pct: 18.2, index_yield_pct: 16.3, yield_over_index_bps: (yBid + yAsk) / 2,
  face_value_rub: 1000, accrued_rub: 12.5, y_idx_slope_bps_per_pct: -52,
  emitter_name: "ТЕСТ", rating: "AA",
});

const ROWS = [
  row("RU000A0TEST1", "ГлубЛикв 001", "2027-06-01", 100.0, 100.2, 200, 190),
  row("RU000A0TEST2", "ТонкийБид 002", "2029-03-15", 99.5, 99.9, 240, 220),
  row("RU000A0TEST3", "Дальний 003", "2032-12-01", 98.0, 98.6, 320, 290),
];

// лестницы: 1-я набирает 10 млн, 2-я — только оффер, 3-я — только ~1 млн бида
const DEPTH = {
  RU000A0TEST1: {
    b: [[100.0, 300], [99.9, 5000], [99.8, 6000]],
    a: [[100.2, 400], [100.3, 4000], [100.5, 8000]],
  },
  RU000A0TEST2: {
    b: [[99.5, 50]],
    a: [[99.9, 2000], [100.0, 9000]],
  },
  RU000A0TEST3: {
    b: [[98.0, 1000], [97.5, 200]],
    a: [[98.6, 60]],
  },
};

function Harness() {
  const [volRub, setVolRub] = useState(1e6);
  const [matFrom, setMatFrom] = useState("");
  const [matTo, setMatTo] = useState("");
  const [twoSided, setTwoSided] = useState(false);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ key: "y_idx_bid_bps", dir: "asc" });
  const [basesSel, setBasesSel] = useState([]);
  const [emittersSel, setEmittersSel] = useState([]);
  const ISSUERS = [{ name: "ТЕСТ", count: 3 }, { name: "РЖД", count: 12 }, { name: "ГТЛК", count: 7 }];
  const toggleIn = (setter) => (v) =>
    setter((a) => (a.includes(v) ? a.filter((x) => x !== v) : [...a, v]));

  const rows = useMemo(() => {
    let out = ROWS.slice();
    if (matFrom) out = out.filter((b) => b.maturity_date && b.maturity_date >= matFrom);
    if (matTo) out = out.filter((b) => b.maturity_date && b.maturity_date <= matTo);
    if (volRub > 0) out = out.map((b) => applyVolume(b, DEPTH[b.isin], volRub)).filter(Boolean);
    if (twoSided) out = out.filter((b) => b.bid_price_pct != null && b.ask_price_pct != null);
    return out;
  }, [volRub, matFrom, matTo, twoSided]);

  return (
    <>
      <Toolbar
        onlyWatch={false} setOnlyWatch={() => {}}
        basesSel={basesSel} toggleBase={toggleIn(setBasesSel)} clearBases={() => setBasesSel([])}
        ratingsSel={[]} toggleRating={() => {}}
        issuers={ISSUERS} emittersSel={emittersSel} toggleEmitter={toggleIn(setEmittersSel)}
        clearEmitters={() => setEmittersSel([])}
        activeFilters={(basesSel.length ? 1 : 0) + (emittersSel.length ? 1 : 0) + (twoSided ? 1 : 0)
          + (volRub > 0 ? 1 : 0) + (matFrom ? 1 : 0) + (matTo ? 1 : 0) + (query ? 1 : 0)}
        onResetFilters={() => { setBasesSel([]); setEmittersSel([]); setTwoSided(false); setVolRub(0); setMatFrom(""); setMatTo(""); setQuery(""); }}
        twoSided={twoSided} setTwoSided={setTwoSided}
        volRub={volRub} setVolRub={setVolRub} depthTs={Date.now() / 1000} depthLoading={false}
        matFrom={matFrom} setMatFrom={setMatFrom} matTo={matTo} setMatTo={setMatTo}
        query={query} setQuery={setQuery} watchCount={0}
        shown={rows.length} total={ROWS.length}
        showAnalytics={false} setShowAnalytics={() => {}}
        visibleCols={DEFAULT_COLS} onToggleCol={() => {}} onResetCols={() => {}} onMoveCol={() => {}}
      />
      <BondTable rows={rows} status="ready" sort={sort}
        onSort={(k) => setSort((s) => ({ key: k, dir: s.dir === "asc" ? "desc" : "asc" }))}
        onOpen={() => {}} watch={[]} onToggleStar={() => {}}
        visibleCols={DEFAULT_COLS} onMoveCol={() => {}} />
    </>
  );
}

createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={new QueryClient()}>
    <div id="app"><Harness /></div>
  </QueryClientProvider>
);
