import { useState, useEffect } from "react";
import { useLocation } from "react-router-dom";
import { fmt } from "../format.js";
import KpisInline from "./Kpis.jsx";
import { IconRefresh } from "./icons.jsx";
import SignalsBell from "./SignalsBell.jsx";
import { usePageStatusItems } from "../pageStatus.jsx";

const THEMES = [["light", "#ffffff", "Светлая"], ["win", "#c0c0c0", "Old internet"], ["dark", "#000000", "Тёмная"]];

function ThemeSwitch({ theme, onSetTheme }) {
  return (
    <span className="status-cell theme-switch" role="group" aria-label="Цветовая гамма">
      {THEMES.map(([v, c, title]) => (
        <button key={v} type="button" title={title} aria-pressed={theme === v}
          className={"theme-dot" + (theme === v ? " on" : "")}
          style={{ background: c }} onClick={() => onSetTheme(v)} />
      ))}
    </span>
  );
}

// Часы: живая секундная стрелка в нижней строке
function Clock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const p = (n) => String(n).padStart(2, "0");
  const on = t.getSeconds() % 2 === 0;
  const sep = <span style={{ opacity: on ? 1 : 0.2 }}>:</span>;
  return (
    <span className="status-cell" aria-label="Время">
      <span className="meta-v">{p(t.getHours())}{sep}{p(t.getMinutes())}{sep}{p(t.getSeconds())}</span>
    </span>
  );
}

// Источники данных одной точкой: строка статусбара не влезала по ширине.
// Цвет — агрегат (все на связи / часть отвалилась / все молчат),
// разбивка по источникам — в тултипе при наведении.
function SourcesDot({ src }) {
  const [open, setOpen] = useState(false);
  const nOn = src.filter((s) => s.on).length;
  const state = nOn === src.length ? "on" : nOn === 0 ? "off" : "part";
  const label = state === "on" ? "все источники на связи"
    : state === "off" ? "нет связи ни с одним источником"
    : `на связи ${nOn} из ${src.length}`;
  return (
    <span className="status-cell src-cell" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
      title={label} aria-label={`Источники данных: ${label}`}>
      <span className={"src-dot " + state} />
      {open && (
        <div className="src-pop">
          {src.map((s) => (
            <div key={s.k} className="src-pop-row">
              <span className={"src-dot " + (s.on ? "on" : "off")} />
              <span className="src-pop-k">{s.k}</span>
              <span className="src-pop-v">{s.on ? "связь активна" : "нет связи"}</span>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}

/** Итоги активной вкладки: их публикует сама вкладка (см. src/pageStatus).
 *  Раньше у каждой была своя полоса над этой — две строки подряд. */
function PageStatusCells() {
  const items = usePageStatusItems();
  if (!items.length) return null;
  return (
    <>
      {items.map((it) => (
        <span key={it.k} className={"status-cell ps-cell" + (it.opt ? " ps-opt" : "")}
          title={it.title || undefined}>
          <span className="ps-k">{it.k}</span>
          <span className={"ps-v" + (it.cls ? " " + it.cls : "")}>{it.v}</span>
        </span>
      ))}
    </>
  );
}

export default function StatusBar({ count, kpiBonds = [], live, sources = {}, theme, onSetTheme,
                                    meta = {}, onRefresh }) {
  // ALOR = живой WS-поток; CBONDS — из meta (кривые ставок построены)
  const src = [
    { k: "ALOR", on: live },
    { k: "CBONDS", on: !!sources.cbonds },
  ];
  const onFloaters = useLocation().pathname.startsWith("/floaters");
  return (
    <footer className="statusbar">
      {onSetTheme && <ThemeSwitch theme={theme} onSetTheme={onSetTheme} />}
      {onFloaters && (
        <span className="status-cell tools-cell" title="инструментов в выборке">
          БУМАГ <span className="counter">{String(count).padStart(3, "0")}</span>
        </span>
      )}
      {onFloaters && <KpisInline bonds={kpiBonds} />}
      <PageStatusCells />
      <span className="status-cell grow" />
      {/* подписи РАСЧЁТ/СТАВКИ ушли в тултип — двух дат хватает, а строка не влезала */}
      <span className="status-cell meta-chip" title="дата расчёта / дата ставок">
        <span className="meta-v">{fmt.date(meta.calc_date) || "—"}</span>
        <span className="meta-sep">/</span>
        <span className="meta-v">{fmt.date(meta.rates_date) || "—"}</span>
      </span>
      <Clock />
      <SourcesDot src={src} />
      <SignalsBell />
      {onRefresh && (
        <span className="status-cell refresh-cell">
          <button className="status-refresh" onClick={onRefresh} title="Обновить" aria-label="Обновить">
            <IconRefresh size={13} />
          </button>
        </span>
      )}
    </footer>
  );
}
