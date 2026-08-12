// Тест-харнесс вкладки СДЕЛКИ: моки /api через подмену fetch. Не входит в
// прод-бандл (отдельный entry, как chart-test). Нужен, чтобы гонять ленту без
// бэкенда и авторизации — раньше её вообще нечем было проверить глазами, и
// поломки всплывали только на проде.
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import TradesTape from "./components/TradesTape.jsx";
import { PageStatusProvider, usePageStatusItems } from "./pageStatus.jsx";
import "./styles.css";

const NAMES = [["RU000A108Q11", "ТестФлоат 1Р3", "KEYRATE"], ["RU000A105SD9", "ОФЗ 29025", "RUONIA"],
               ["RU000A106K43", "РЖД 1Р-40R", "FIXED"], ["RU000A107HR8", "Систем2P14", "KEYRATE"]];

const trades = (n) => Array.from({ length: n }, (_, i) => {
  const [isin, name, base] = NAMES[i % NAMES.length];
  const ndm = i % 7 === 0;
  return {
    trade_id: 1000 + i, isin, name, emitter: "Тестовый эмитент", base,
    rating: ["AAA", "AA", "A"][i % 3],
    ts: `2026-08-12 1${7 - (i % 8)}:${String(59 - (i % 60)).padStart(2, "0")}:20`,
    price: 100 + (i % 9) / 100, qty: 1000 + i, value: 1e6 + i * 73000,
    side: ndm ? null : (i % 2 ? "buy" : "sell"),
    board: ndm ? "PSOB" : "TQCB", board_title: ndm ? "РПС" : "Безадресный: корп.",
    board_short: ndm ? "РПС" : "Т+", negotiated: ndm, market: ndm ? "ndm" : "bonds",
    cur: "SUR", yld: 18 + (i % 5) / 10, y_idx_bps: 150 + (i % 40),
    dm_bps: 160 + (i % 30), maturity: "2029-03-14", margin_bps: 230, coupons_per_year: 12,
  };
});

const ROWS = trades(120);

const json = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });

window.fetch = (url) => {
  const u = String(url);
  if (u.includes("/api/trades/issuers")) {
    return json({ issuers: [{ name: "Тестовый эмитент", count: 4 }, { name: "РЖД", count: 12 }] });
  }
  if (u.includes("/api/trades/ratings")) {
    return json({ ratings: [{ name: "AAA", count: 259 }, { name: "AA", count: 140 },
                            { name: "A", count: 123 }, { name: "BBB", count: 52 }] });
  }
  if (u.includes("/api/blocks/days")) {
    return json({ from: "2026-07-13", days: 30, rows: ROWS.slice(0, 20).map((r, i) => ({
      isin: r.isin, name: r.name, emitter: r.emitter, date: `2026-08-${String(12 - (i % 10)).padStart(2, "0")}`,
      board: "PSOB", board_title: "РПС", board_short: "РПС", numtrades: 2 + i,
      value: 5e6 + i * 1e6, waprice: 100 + i / 50, volume: 5000 + i,
    })) });
  }
  if (u.includes("/api/trades")) {
    const lim = Number(new URL(u, location.origin).searchParams.get("limit")) || 500;
    const rows = ROWS.slice(0, Math.min(lim, ROWS.length));
    return json({
      from: "2026-08-05", days: 7, trades: rows, truncated: rows.length < ROWS.length,
      summary: { n: ROWS.length, value: 29271400000, buy_value: 6845800000,
        sell_value: 7738100000, by_market: { ndm: { n: 17, value: 14687500000 } },
        top: NAMES.map(([isin, name], i) => ({ isin, name, n: 40 - i * 7, value: 4.2e9 - i * 1e9 })),
        archive_till: "2026-08-12 18:38:11" },
    });
  }
  return json({});
};

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

// нижняя полоса приложения в миниатюре: сюда лента публикует свои итоги
function FakeStatusBar() {
  const items = usePageStatusItems();
  return (
    <footer className="statusbar">
      <span className="status-cell theme-switch">
        <button className="theme-dot" style={{ background: "#fff" }} />
        <button className="theme-dot on" style={{ background: "#000" }} />
      </span>
      {items.map((it) => (
        <span key={it.k} className={"status-cell ps-cell" + (it.opt ? " ps-opt" : "")} title={it.title}>
          <span className="ps-k">{it.k}</span>
          <span className={"ps-v" + (it.cls ? " " + it.cls : "")}>{it.v}</span>
        </span>
      ))}
      <span className="status-cell grow" />
      <span className="status-cell meta-chip"><span className="meta-v">12.08.2026</span></span>
    </footer>
  );
}

createRoot(document.getElementById("root")).render(
  <PageStatusProvider>
    <div id="app" className="theme-dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/trades"]}>
          <Routes>
            <Route path="/trades" element={<TradesTape />} />
            <Route path="/chart/:isin" element={<div className="ia-empty">график</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
      <FakeStatusBar />
    </div>
  </PageStatusProvider>
);
