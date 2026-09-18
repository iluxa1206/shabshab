/**
 * Стенд шапки: эмодзи вкладок (SVG Twemoji) в трёх темах рядом, плюс теги
 * режима адресных сделок (РПС / Р / В) как они стоят в панели карточки и в
 * общей ленте. Смысл — увидеть, как значки сидят в строке и не пестрят ли.
 * Открывать: npm run dev → /nav-test.html
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Topbar from "./components/Topbar.jsx";
import "./styles.css";

const THEMES = [["", "light"], ["theme-dark", "dark"], ["theme-win", "win"]];
const qc = new QueryClient();

const Tags = () => (
  <table className="bt-table" style={{ margin: "8px 16px" }}>
    <tbody>
      <tr className="bt-row bt-buy"><td className="bt-side">buy</td><td>безадресная</td></tr>
      <tr className="bt-row bt-ndm"><td className="bt-side">РПС</td><td>адресная (PSOB)</td></tr>
      <tr className="bt-row bt-ndm"><td className="bt-side bt-plc">Р</td><td>размещение (PSAU)</td></tr>
      <tr className="bt-row bt-ndm"><td className="bt-side bt-bb">В</td><td>выкуп (PSBB)</td></tr>
      <tr><td colSpan={2} style={{ padding: "6px 0" }}>
        <span className="blk-tag">Т+</span>{" "}
        <span className="blk-tag blk-tag-ndm">РПС</span>{" "}
        <span className="blk-tag blk-tag-ndm blk-tag-plc">Размещ.</span>
      </td></tr>
    </tbody>
  </table>
);

createRoot(document.getElementById("root")).render(
  <QueryClientProvider client={qc}>
    {THEMES.map(([cls, name]) => (
      <div key={name} className={cls} style={{ background: "var(--bg)", color: "var(--fg)", padding: "0 0 12px" }}>
        {["/floaters", "/fixed"].map((path) => (
          <MemoryRouter key={path} initialEntries={[path]}>
            <Topbar user={{ email: `${name}@desk`, role: "user" }} onLogout={() => {}} onOpenSettings={() => {}} />
          </MemoryRouter>
        ))}
        <Tags />
      </div>
    ))}
  </QueryClientProvider>,
);
