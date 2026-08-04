// Тест-харнесс схемы метрик карточки (ValCards): 6 плиток без бэкенда и логина.
// Ставит рядом рыночный блок и оба состояния калькулятора (введённая цена /
// прошлая дата) на ширине ящика (680px) и на узкой — видно перенос сетки.
// Не входит в прод-бандл (отдельный entry, как chart-test / analytics-test).
import { createRoot } from "react-dom/client";
import { ValCards } from "./components/Drawer.jsx";
import "./styles.css";

const MARKET = {
  yield_over_index_bps: 198, yield_xirr_pct: 16.412, index_yield_pct: 14.407,
  clean_price_pct: 100.41, dirty_price_rub: 1012.85, accrued_settle_rub: 8.44,
  settlement_date: "2026-08-05",
};
const CALC = { ...MARKET, yield_over_index_bps: -37, yield_xirr_pct: 14.035,
  clean_price_pct: 104.9, dirty_price_rub: 1057.44 };
const PAST = { ...MARKET, yield_over_index_bps: 201, yield_xirr_pct: 16.6,
  clean_price_pct: 100.42, dirty_price_rub: 1012.11, accrued_settle_rub: 7.91,
  settlement_date: "2026-07-31" };
// крупные числа: бумага с номиналом 10 млн (RU000A1034Q5) — проверка переноса
const BIG = { ...MARKET, yield_over_index_bps: 1240, clean_price_pct: 69.0,
  dirty_price_rub: 7285000, accrued_settle_rub: 385000 };

function Block({ title, children }) {
  return (
    <div style={{ marginBottom: 28 }}>
      <div className="section-title">{title}</div>
      {children}
    </div>
  );
}

function App() {
  return (
    <div style={{ padding: 24, display: "flex", gap: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
      <div style={{ width: 680 - 48, background: "var(--bg)" }}>
        <Block title="Ящик 680px · рынок"><ValCards v={MARKET} priceDate="2026-08-04" /></Block>
        <Block title="Ящик 680px · под введённую цену"><ValCards v={CALC} priceDate="2026-08-04" calc /></Block>
        <Block title="Ящик 680px · прошлая дата"><ValCards v={PAST} priceDate="2026-07-30" calc /></Block>
        <Block title="Ящик 680px · номинал 10 млн"><ValCards v={BIG} priceDate="2026-08-04" /></Block>
      </div>
      <div style={{ width: 360 }}>
        <Block title="Узкий 360px"><ValCards v={MARKET} priceDate="2026-08-04" /></Block>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
