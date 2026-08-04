// Тест-харнесс вкладки «Индекс RUONIA»: боевой ответ /api/curves/ruonia-index
// зашит в мок, fetch подменён — вкладка открывается без бэкенда и логина.
// Не входит в прод-бандл (отдельный entry, как chart-test / valcards-test).
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import CurvesModule from "./components/CurvesModule.jsx";
import "./styles.css";

const PAYLOAD = {"start_date": "2010-01-11", "start_value": 1.0, "last_date": "2026-08-04", "last_index": 4.42266405585164, "anchor_date": "2026-07-16", "anchor_value": 4.39019567778528, "max_gap_bps": 0.0, "rows": [{"date": "2026-08-04", "rate_pct": 13.86, "is_fixing": false, "index_fact": 4.42266405585164, "index_calc": 4.42266406, "gap_bps": 0.0}, {"date": "2026-08-03", "rate_pct": 13.86, "is_fixing": true, "index_fact": 4.42098529266928, "index_calc": 4.42098529, "gap_bps": -0.0}, {"date": "2026-08-02", "rate_pct": 13.95, "is_fixing": false, "index_fact": 4.41929756353589, "index_calc": 4.41929756, "gap_bps": -0.0}, {"date": "2026-08-01", "rate_pct": 13.95, "is_fixing": false, "index_fact": 4.41760983440251, "index_calc": 4.41760983, "gap_bps": -0.0}, {"date": "2026-07-31", "rate_pct": 13.95, "is_fixing": true, "index_fact": 4.41592210526913, "index_calc": 4.41592211, "gap_bps": 0.0}, {"date": "2026-07-30", "rate_pct": 13.99, "is_fixing": true, "index_fact": 4.41423018526113, "index_calc": 4.41423019, "gap_bps": 0.0}, {"date": "2026-07-29", "rate_pct": 13.9, "is_fixing": true, "index_fact": 4.41254978958784, "index_calc": 4.41254979, "gap_bps": 0.0}, {"date": "2026-07-28", "rate_pct": 13.7, "is_fixing": true, "index_fact": 4.410894193685, "index_calc": 4.41089419, "gap_bps": -0.0}, {"date": "2026-07-27", "rate_pct": 13.69, "is_fixing": true, "index_fact": 4.40924042652229, "index_calc": 4.40924043, "gap_bps": 0.0}, {"date": "2026-07-26", "rate_pct": 13.88, "is_fixing": false, "index_fact": 4.40756561780762, "index_calc": 4.40756562, "gap_bps": 0.0}, {"date": "2026-07-25", "rate_pct": 13.88, "is_fixing": false, "index_fact": 4.40589080909296, "index_calc": 4.40589081, "gap_bps": 0.0}, {"date": "2026-07-24", "rate_pct": 13.88, "is_fixing": true, "index_fact": 4.40421600037829, "index_calc": 4.404216, "gap_bps": -0.0}, {"date": "2026-07-23", "rate_pct": 14.03, "is_fixing": true, "index_fact": 4.40252374262188, "index_calc": 4.40252374, "gap_bps": -0.0}, {"date": "2026-07-22", "rate_pct": 14.22, "is_fixing": true, "index_fact": 4.40080923557175, "index_calc": 4.40080924, "gap_bps": 0.0}, {"date": "2026-07-21", "rate_pct": 14.57, "is_fixing": true, "index_fact": 4.39905322993996, "index_calc": 4.39905323, "gap_bps": 0.0}, {"date": "2026-07-20", "rate_pct": 14.75, "is_fixing": true, "index_fact": 4.3972762484423, "index_calc": 4.39727625, "gap_bps": 0.0}, {"date": "2026-07-19", "rate_pct": 14.74, "is_fixing": false, "index_fact": 4.39550262044034, "index_calc": 4.39550262, "gap_bps": -0.0}, {"date": "2026-07-18", "rate_pct": 14.74, "is_fixing": false, "index_fact": 4.39372899243837, "index_calc": 4.39372899, "gap_bps": -0.0}, {"date": "2026-07-17", "rate_pct": 14.74, "is_fixing": true, "index_fact": 4.3919553644364, "index_calc": 4.39195536, "gap_bps": -0.0}, {"date": "2026-07-16", "rate_pct": 14.63, "is_fixing": true, "index_fact": 4.39019567778528, "index_calc": 4.39019568, "gap_bps": 0.0}]};
window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(PAYLOAD) });

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={qc}>
    <MemoryRouter initialEntries={["/curves/ruonia"]}>
      <Routes><Route path="/curves/:view" element={<CurvesModule />} /></Routes>
    </MemoryRouter>
  </QueryClientProvider>
);
