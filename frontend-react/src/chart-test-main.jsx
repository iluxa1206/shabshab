// Тест-харнесс ChartPage: моки /api через подмену fetch, синтетические свечи и
// история Y-IDX. Не входит в прод-бандл (отдельный entry, как analytics-test).
// Нужен, чтобы гонять полноэкранный график без бэкенда и авторизации.
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ChartPage from "./components/ChartPage.jsx";
import "./styles.css";

const ISIN = "RU000A108Q11";

// случайное блуждание вокруг 100% — 400 дневных свечей
function mkCandles(n, stepMin) {
  const out = [];
  let px = 100.2;
  const t0 = Date.parse("2025-06-02T10:00:00Z");
  for (let i = 0; i < n; i++) {
    px += (Math.sin(i / 9) + (i % 7) / 7 - 0.5) * 0.12;
    const o = px, c = px + ((i % 5) - 2) * 0.05;
    const d = new Date(t0 + i * (stepMin ? stepMin * 60000 : 864e5));
    const t = stepMin
      ? d.toISOString().slice(0, 19).replace("T", " ")
      : d.toISOString().slice(0, 10) + " 00:00:00";
    out.push({ t, o: +o.toFixed(2), h: +(Math.max(o, c) + 0.08).toFixed(2),
      l: +(Math.min(o, c) - 0.08).toFixed(2), c: +c.toFixed(2), v: 1000 + (i % 13) * 320 });
  }
  return out;
}

const CANDLES = { "1d": mkCandles(400), "1w": mkCandles(200), "1h": mkCandles(300, 60), "5m": mkCandles(280, 5) };

const spreadPoints = () => mkCandles(400).map((c, i) => ({
  date: c.t.slice(0, 10), price: c.c, y_idx_bps: Math.round(180 + Math.sin(i / 25) * 45 + (i % 9)),
  dm_bps: Math.round(200 + Math.sin(i / 25) * 40), ytm: 21.4, src: i > 380 ? "est" : "honest",
}));

const json = (body) => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve(body),
});

window.fetch = (url) => {
  const u = String(url);
  if (u.includes("/candles")) {
    const tf = new URL(u, location.origin).searchParams.get("tf") || "1d";
    return json({ isin: ISIN, tf, candles: CANDLES[tf] || CANDLES["1d"] });
  }
  if (u.includes("/spread")) {
    const from = new URL(u, location.origin).searchParams.get("from");
    const pts = spreadPoints().filter((p) => !from || p.date >= from);
    return json({ isin: ISIN, kind: "floater", exact_from: pts[0]?.date, points: pts });
  }
  if (u.includes("/api/bonds?")) {
    return json({ items: [{ isin: ISIN, short_name: "ТестФлоат 1Р3", emitter_name: "Тестовый эмитент",
      rating: "AA", disc_margin_bps: 214, spread_dur_yrs: 2.4, yield_over_index_bps: 186 }], total: 1 });
  }
  if (u.includes("/api/bonds/")) {
    return json({
      reference: { isin: ISIN, short_name: "ТестФлоат 1Р3", base_rate_type: "KEYRATE",
        spread_bps: 230, formula: "КС + 230", maturity_date: "2029-04-18", face_value: 1000,
        face_unit: "RUB", accrued_interest: 12.3 },
      market: { last_price_pct: 100.42, price_source: "moex", calc_date: "2026-08-03",
        rates_date: "2026-08-03", market_timestamp: "2026-08-03T18:45:00" },
      valuation: { clean_price_pct: 100.42, dm_bps: 208, sm_bps: 197, disc_margin_bps: 214,
        yield_over_index_bps: 186, yield_xirr_pct: 21.9, index_yield_pct: 20.1, pricing_status: "ok" },
      cashflow: [], floater: { spread_duration_yrs: 2.41, rate_duration_yrs: 0.18, days_to_refix: 44 },
      sources: {}, warnings: [],
    });
  }
  return json({});
};

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

createRoot(document.getElementById("root")).render(
  <div id="app">
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/chart/${ISIN}?p=3m`]}>
        <Routes>
          <Route path="/chart/:isin" element={<ChartPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  </div>
);
