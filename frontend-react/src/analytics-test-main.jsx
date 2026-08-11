// Тест-харнесс AnalyticsPanel: мок watchlist из 12 бумаг (почти все эмитенты
// одиночные) — репро скрина юзера. Не входит в прод-бандл (отдельный entry).
import { createRoot } from "react-dom/client";
import { useState } from "react";
import AnalyticsPanel, { focusMatch } from "./components/AnalyticsPanel.jsx";
import "./styles.css";

const B = (isin, name, emitter, rating, y, dur, refix) => ({
  isin, short_name: name, emitter_name: emitter, rating,
  yield_over_index_bps: y, disc_margin_bps: y - 8, spread_dur_yrs: dur, days_to_refix: refix,
});

const rows = [
  B("RU1", "ДОМ 2Р5", "ДОМ.РФ", "AAA", 63, 0.2, 12),
  B("RU2", "ГазпКапЗР6", "Газпром капитал", "AAA", 122, 1.2, 25),
  B("RU3", "ГазпКапЗР7", "Газпром капитал", "AAA", 128, 2.8, 40),
  B("RU4", "sСОПФДОМ6", "СОПФ ДОМ.РФ", "AAA", 170, 1.1, 33),
  B("RU5", "РусГидБП12", "РусГидро", "AAA", 155, 2.2, 8),
  B("RU6", "ГазпромК07", "Газпром капитал", "AAA", 196, 1.7, 61),
  B("RU7", "ВЭБР-40", "ВЭБ.РФ", "AAA", 222, 5.7, 45),
  B("RU8", "ЕАБР ПЗ-07", "ЕАБР", "AAA", 293, 1.8, 30),
  B("RU9", "Росагрл1Р5", "Росагролизинг", "AA", 325, 2.9, 55),
  B("RU10", "БалтЛизП11", "Балтийский лизинг БО", "AA", 649, 0.9, 70),
  B("RU11", "БалтЛизП12", "Балтийский лизинг БО", "AA", 677, 1.4, 85),
  B("RU12", "Африка МЛТ", "Мировые ЛизТехнологии", "BB", 540, 2.1, 200),
];

// мок /api/history/aggregate/yidx — бэкенда в харнессе нет
const mkSeries = (keys, days, base) => {
  const today = new Date("2026-07-30");
  const dates = Array.from({ length: Math.min(days, 14) }, (_, i) => {
    const d = new Date(today); d.setDate(d.getDate() - (Math.min(days, 14) - 1 - i));
    return d.toISOString().slice(0, 10);
  });
  return {
    by: "x", days, dates, exact_from: dates[0],
    series: keys.map((k, ki) => ({
      key: k,
      points: dates.map((d, i) => ({ date: d, med: base[ki] + Math.sin(i / 2 + ki) * 18, n: 5 + ki })),
    })),
  };
};
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
  const u = String(url);
  if (u.includes("/api/history/aggregate/yidx")) {
    const req = JSON.parse(opts?.body || "{}");
    const days = req.days || 91;
    console.log("yidx mock: isins filter size =", (req.isins || []).length);
    const body = req.by === "issuer"
      ? mkSeries(["Газпром капитал", "ДОМ.РФ", "Балтийский лизинг БО", "РЫНОК"], days, [150, 90, 620, 210])
      : mkSeries(["AAA", "AA", "BB"], days, [120, 300, 560]);
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  }
  return realFetch(url, opts);
};

// имитация App: focus живёт снаружи и сужает «таблицу» под панелью
function Harness() {
  const [focus, setFocus] = useState(null);
  const m = focusMatch(focus);
  const shown = m ? rows.filter(m) : rows;
  return (
    <>
      <AnalyticsPanel rows={rows} focus={focus} onFocus={setFocus} />
      <div id="tbl" style={{ font: "12px monospace", color: "#ccc", padding: "8px 16px" }}>
        таблица: {shown.length}/{rows.length} — {shown.map((b) => b.short_name).join(", ")}
      </div>
    </>
  );
}

createRoot(document.getElementById("root")).render(<Harness />);
