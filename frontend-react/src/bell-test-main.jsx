/**
 * Стенд плашек и заливки строк ленты сигналов: все виды события рядом, в трёх
 * темах сразу. Смысл — увидеть цвет, а не читать код: заливка обязана быть
 * различима, но не спорить с подсветкой сработавшего алерта.
 * Открывать: npm run dev → /bell-test.html
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { eventTag, sideInfo, tradeMode, tradeTone } from "./signalFormat.js";
import "./styles.css";

const EVENTS = [
  { id: 1, name: "РЖД 1Р-54R", reason: "block", side: "buy", negotiated: false, price: 100.12, val_bps: 212 },
  { id: 2, name: "ВЭБP-41", reason: "block", side: "sell", negotiated: false, price: 99.4, val_bps: 180 },
  { id: 3, name: "ГТЛК 2P-01", reason: "block", side: null, negotiated: true, price: 98.8, val_bps: 340 },
  { id: 4, name: "ПозитивР4", reason: "new", side: "ask", price: 100.5, val_bps: 160 },
  { id: 5, name: "ДОМ 2P10", reason: "spread", side: "bid", price: 100.1, val_bps: 120 },
  { id: 6, name: "МегаФн2Р14", reason: "money", side: "ask", price: 99.9, val_bps: 130 },
];

function Row({ e }) {
  const tone = tradeTone(e);
  return (
    <button type="button" className={"sb-row" + (tone ? " sb-t-" + tone : "")}>
      <span className="sb-row-1">
        <span className="sb-name">{e.name}</span>
        <span className={"sb-tag sb-" + e.reason}>{eventTag(e)}</span>
        <span className="sb-time num">14:32</span>
      </span>
      <span className="sb-row-2 num">
        <span className={sideInfo(e).cls}>{sideInfo(e).text}</span>
        <b><span className="sb-k">R-spread</span> {e.val_bps} бп</b>
        <span className="sb-px">{e.price}%</span>
        <span className="sb-vol">62,4 млн</span>
      </span>
      <span className="sb-row-mode">{[tradeMode(e), "погашение 2,7 г"].filter(Boolean).join(" · ")}</span>
      <span className="sb-row-3">крупная сделка · RU000A10FV69</span>
    </button>
  );
}

const THEMES = [["", "light"], ["theme-dark", "dark"], ["theme-win", "win"]];

createRoot(document.getElementById("root")).render(
  <div style={{ display: "flex", gap: 20, padding: 16, alignItems: "flex-start" }}>
    {THEMES.map(([cls, label]) => (
      <div key={label} className={cls} style={{ background: "var(--bg)", padding: 10 }}>
        <div style={{ font: "700 11px var(--mono)", color: "var(--mut)", marginBottom: 6 }}>{label}</div>
        <div className="sb-list" style={{ width: 360, border: "1px solid var(--line)" }}>
          {EVENTS.map((e) => <Row key={e.id} e={e} />)}
        </div>
      </div>
    ))}
  </div>
);
