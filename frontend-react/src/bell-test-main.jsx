/**
 * Стенд плашек и заливки строк ленты сигналов: все виды события рядом, в трёх
 * темах сразу. Смысл — увидеть цвет, а не читать код: заливка обязана быть
 * различима, но не спорить с подсветкой сработавшего алерта.
 * Открывать: npm run dev → /bell-test.html
 */
import React from "react";
import { createRoot } from "react-dom/client";
import SignalEventRow from "./components/SignalEventRow.jsx";
import BestWorm from "./components/BestWorm.jsx";
import "./styles.css";

// поля ровно те, что приходят с бэка: строка стенда обязана быть той же, что
// в ленте (SignalEventRow), иначе стенд врёт
const EVENTS = [
  { id: 1, name: "РЖД 1Р-54R", reason: "block", side: "buy", negotiated: false, price: 100.12, val_bps: 212, prev_val_bps: 197, money_rub: 62.4e6, years: 2.7, fired_at: "2026-08-28T14:32:00", isin: "RU000A10FV69", maturity: "2029-05-14" },
  { id: 2, name: "ВЭБP-41", reason: "block", side: "sell", negotiated: false, price: 99.4, val_bps: 180, money_rub: 310e6, years: 4.2, fired_at: "2026-08-28T14:31:00", isin: "RU000A10ABC1", maturity: "2030-11-02" },
  { id: 3, name: "ГТЛК 2P-01", reason: "block", side: null, negotiated: true, price: 98.8, val_bps: 340, money_rub: 900e6, years: 0.6, fired_at: "2026-08-28T14:28:00", isin: "RU000A10ABC2", maturity: "2027-03-20" },
  { id: 4, name: "ПозитивР4", reason: "new", side: "ask", price: 100.5, val_bps: 160, money_ok_rub: 25e6, levels: 3, best: true, years: 1.4, filter_name: "Мой фильтр КС", fired_at: "2026-08-28T14:20:00", isin: "RU000A10ABC3", maturity: "2027-12-01" },
  { id: 5, name: "ДОМ 2P10", reason: "spread", side: "bid", price: 100.1, val_bps: 120, prev_val_bps: 105, prev_price: 99.98, money_ok_rub: 48e6, levels: 2, years: 3.1, filter_name: "Мой фильтр КС", fired_at: "2026-08-28T14:12:00", isin: "RU000A10ABC4", maturity: "2029-09-15" },
  { id: 6, name: "МегаФн2Р14", reason: "money", side: "ask", price: 99.9, val_bps: 130, money_ok_rub: 77e6, prev_money_ok_rub: 59e6, levels: 5, best: true, years: 2.0, filter_name: "Объём 50 млн", fired_at: "2026-08-28T14:05:00", isin: "RU000A10ABC5", maturity: "2028-08-30" },
];

const THEMES = [["", "light"], ["theme-dark", "dark"], ["theme-win", "win"]];

// ЧЕРВЯЧОК крупно — проверить анимацию и глаз, в строке он 18×10
const Zoom = () => (
  <div style={{ padding: "8px 16px", display: "flex", gap: 14, alignItems: "center" }}>
    {[3, 6].map((k) => (
      <span key={k} style={{ transform: `scale(${k})`, transformOrigin: "left center",
                             display: "inline-block", width: 18 * k, height: 10 * k }}>
        <BestWorm />
      </span>
    ))}
  </div>
);

createRoot(document.getElementById("root")).render(
  <div>
  <Zoom />
  <div style={{ display: "flex", gap: 20, padding: 16, alignItems: "flex-start" }}>
    {THEMES.map(([cls, label]) => (
      <div key={label} className={cls} style={{ background: "var(--bg)", padding: 10 }}>
        <div style={{ font: "700 11px var(--mono)", color: "var(--mut)", marginBottom: 6 }}>{label}</div>
        <div className="sb-list" style={{ width: 560, border: "1px solid var(--line)" }}>
          {EVENTS.map((e) => <SignalEventRow key={e.id} e={e} />)}
        </div>
      </div>
    ))}
  </div>
  </div>
);
