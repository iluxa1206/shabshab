// Харнесс блоб-аватара: макет строки шапки (56px) — видно, следит ли он
// взглядом, не растёт ли строка и на одном ли уровне кнопки. Ниже — вариации:
// семя (name) меняет морду, expression/hue/background — настроение, цвет, подложку.
//
// expression — ИМПОРТИРУЕМОЕ ЗНАЧЕНИЕ, не строка: пакет не тянет все 14 поз в
// бандл, каждая приезжает только если её импортировали. Строкой компонента падает.
import { createRoot } from "react-dom/client";
import { Blobatar } from "@blobatar/react";
import * as EX from "blobatar/expression";
import "blobatar/motion.css";
import "./styles.css";
import BrandBlobatar from "./components/Blobatar.jsx";

const SEEDS = ["desk", "desk2", "desk3", "floaters", "astra", "bond", "spread", "ruonia",
               "kc", "alor", "moex", "vwap"];
const EXPR = ["idle", "happy", "smug", "sleepy", "thinking", "unsure", "wink", "surprised",
              "mad", "sad", "scared", "love"];
const HUES = [null, 0, 30, 90, 150, 200, 260, 310];

const Cell = ({ label, children }) => (
  <div style={{ display: "grid", justifyItems: "center", gap: 4, width: 92 }}>
    {children}
    <span style={{ fontSize: 10, color: "var(--mut)" }}>{label}</span>
  </div>
);

const Row = ({ title, children }) => (
  <section style={{ padding: "18px 24px", borderBottom: "1px solid var(--line)" }}>
    <div style={{ fontSize: 11, letterSpacing: "0.14em", textTransform: "uppercase",
                  color: "var(--mut)", marginBottom: 12 }}>{title}</div>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>{children}</div>
  </section>
);

createRoot(document.getElementById("root")).render(
  <>
    <header className="menubar">
      <div className="brand-row">
        <BrandBlobatar />
        <span className="seg module-seg" role="tablist">
          <button className="seg-btn">Монитор</button>
          <button className="seg-btn">Сравнение</button>
        </span>
        <span className="menubar-tools"><button className="tool-btn">Аналитика</button></span>
      </div>
    </header>

    <Row title="Семя (name) — из него считаются и форма, и цвет">
      {SEEDS.map((s) => (
        <Cell key={s} label={s}><Blobatar name={s} size={56} animate="always" /></Cell>
      ))}
    </Row>

    <Row title="Выражение (expression), семя desk">
      {EXPR.filter((e) => EX[e]).map((e) => (
        <Cell key={e} label={e}>
          <Blobatar name="desk" size={56} expression={EX[e]} animate="always" />
        </Cell>
      ))}
    </Row>

    <Row title="Цвет (hue), семя desk — форма та же">
      {HUES.map((h) => (
        <Cell key={String(h)} label={h == null ? "из семени" : `hue ${h}`}>
          <Blobatar name="desk" size={56} hue={h ?? undefined} animate="always" />
        </Cell>
      ))}
    </Row>

    <Row title="Подложка (background), семя desk">
      {["none", "square", "circle", "squircle"].map((b) => (
        <Cell key={b} label={b}>
          <Blobatar name="desk" size={56} background={b} animate="always" />
        </Cell>
      ))}
    </Row>
  </>
);
