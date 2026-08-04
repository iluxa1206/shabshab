// Тест-харнесс окна «Фиксинг по дням»: мок /api/bonds/*/coupon-days через
// подмену fetch, без бэкенда и логина. Ряды закрывают выходные (пятничный
// фиксинг работает сб/вс/пн) и стык факт → форвард, чтобы глазами проверить
// колонку расчётного индекса. Не входит в прод-бандл (отдельный entry).
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DayRatesModal } from "./components/BondAudit.jsx";
import "./styles.css";

// та же арифметика, что services.bond_audit.accrue_index — считаем прямо тут,
// чтобы мок был честным (фиксинг дня даёт прирост следующего, выходные простыми)
const OFF = (iso) => [0, 6].includes((new Date(iso + "T00:00:00Z").getUTCDay() + 6) % 7 >= 5 ? 6 : 0)
  || [5, 6].includes((new Date(iso + "T00:00:00Z").getUTCDay() + 6) % 7);

function mkRows(spec) {
  let level = 1, base = 1, prev = null;
  return spec.map(([day, rate, src, close, yidx]) => {
    if (prev != null) level += base * (prev / 100) / 365;
    const row = { day, obs_date: day, rate_pct: rate, src, close_pct: close,
      y_idx_bps: yidx, index: Math.round(level * 1e10) / 1e10 };
    if (!OFF(day)) base = level;
    prev = rate;
    return row;
  });
}

const ROWS = mkRows([
  ["2026-08-05", 13.86, "fact", 100.41, 198],
  ["2026-08-06", 13.90, "fact", 100.44, 195],
  ["2026-08-07", 14.25, "fact", 100.40, 201],   // пятница — её фиксинг на 3 дня
  ["2026-08-08", 14.25, "fact", null, null],    // суббота
  ["2026-08-09", 14.25, "fact", null, null],    // воскресенье
  ["2026-08-10", 15.00, "fact", 100.39, 199],   // понедельник — новый фиксинг
  ["2026-08-11", 15.00, "fact", 100.42, 197],
  ["2026-08-12", 14.9465, "forward", null, null],
  ["2026-08-13", 14.9465, "forward", null, null],
]);

const PAYLOAD = {
  isin: "RU000A106S29", calc_date: "2026-08-11", base: "RUONIA",
  spec: { mode: "average", lag: 7, lag_unit: "cal", avg_window_days: null,
    compounded: null, margin_bps: 130, cap_pct: null, floor_pct: null },
  coupons: [{
    n: 12, start: "2026-08-05", end: "2026-08-14", pay_date: "2026-08-14",
    mean_pct: 14.4437, projected_pct: 14.4437, coupon_rate_pct: 15.7437,
    display_rate_pct: 15.7437, n_fact: 7,
    index_end: ROWS[ROWS.length - 1].index,
    index_rate_pct: Math.round((ROWS[ROWS.length - 1].index - 1) * 365 / 9 * 1e6) / 1e4,
    rows: ROWS,
  }],
  n_days: ROWS.length,
};

window.fetch = (url) => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve(PAYLOAD),
});

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={qc}>
    <DayRatesModal isin="RU000A106S29" onClose={() => {}} />
  </QueryClientProvider>
);
