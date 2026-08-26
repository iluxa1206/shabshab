/**
 * Стенд пикера фильтров ленты срабатываний: список открыт, видно расшифровки
 * и счётчики. Открывать: npm run dev → /sigpick-test.html
 */
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { FeedPicker } from "./components/SignalsModule.jsx";
import "./styles.css";

const OPTIONS = [
  { id: 1, kind: "book", name: "ВДО дешёвые", n: 12,
    note: "рейтинг BB/B, весь рынок без ОФЗ, без субордов" },
  { id: 2, kind: "book", name: "мои эмитенты", n: 3,
    note: "Балтийский лизинг или ГТЛК или 4 эмитентов" },
  { id: 7, kind: "block", name: "сделки Ф5", n: 107,
    note: "от 1 млн · биржевые · КС/RUONIA · R-spread 150…600 бп" },
  { id: 9, kind: "block", name: "Р5", n: 4,
    note: "от 50 млн · все режимы · любая база" },
  { id: 0, kind: "block", name: "умолчание", n: 31,
    note: "звонок по порогу без заведённого фильтра" },
];

function Demo() {
  const [sel, setSel] = useState(new Set([9]));
  return (
    <div style={{ padding: 16, background: "var(--bg)", minHeight: "100vh" }}>
      <div className="sig-head" style={{ width: 620 }}>
        Лента срабатываний
        {sel.size > 0 && <span className="sig-head-sub">4 из 157</span>}
        <FeedPicker options={OPTIONS} sel={sel} onChange={setSel} />
        <button className="btn sig-clear">Очистить</button>
      </div>
      <div className="sig-empty" style={{ width: 620 }}>
        выбрано: {sel.size ? [...sel].join(", ") : "все"}
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<Demo />);
