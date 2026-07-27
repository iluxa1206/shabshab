import { useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchAlerts, deleteAlert } from "../api.js";
import { fmt } from "../format.js";

const THEMES = [["light", "#ffffff", "Светлая"], ["grey", "#3a3f47", "Серая"], ["dark", "#000000", "Тёмная"]];

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

// Плашка алертов: активные + сработавшие. При fired — красная. Hover → список.
function AlertsCell() {
  const qc = useQueryClient();
  const [, setSearchParams] = useSearchParams();
  const q = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts, refetchInterval: 8000 });
  const [open, setOpen] = useState(false);
  const alerts = q.data || [];
  const active = alerts.filter((a) => a.status === "active");
  const fired = alerts.filter((a) => a.status === "fired");
  const hasFired = fired.length > 0;
  const delMut = useMutation({
    mutationFn: (id) => deleteAlert(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  // клик по алерту → карточка + стакан (ob=1 авто-открывает панель стакана)
  const openBond = (a) => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      n.set("isin", a.isin);
      if (a.kind === "fixed") n.set("k", "fixed"); else n.delete("k");
      n.set("ob", "1");
      return n;
    });
    setOpen(false);
  };

  if (!active.length && !fired.length) return null;
  const shown = [...fired, ...active];

  return (
    <span className={"status-cell al-chip" + (hasFired ? " fired" : "")}
      onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      🔔 {active.length}{hasFired && <span className="al-chip-fired"> · {fired.length} ⚠</span>}
      {open && (
        <div className="al-pop" onMouseLeave={() => setOpen(false)}>
          <div className="al-pop-h">Алерты · активных {active.length}{hasFired && `, сработало ${fired.length}`}</div>
          {shown.map((a) => (
            <div key={a.id} className={"al-pop-row" + (a.status === "fired" ? " fired" : "")}
              onClick={() => openBond(a)} title="Открыть карточку и стакан">
              <span className={"al-side al-" + a.side}>{a.side === "buy" ? "покуп" : "прод"}</span>
              <span className="al-pop-isin">{a.isin}</span>
              <span className="al-pop-cond">{a.metric} {a.op} {fmt.num(a.threshold, 2)}</span>
              <span className="al-pop-st">{a.status === "fired" ? `⚠ ${fmt.pct(a.fired_price)}%` : "ждёт"}</span>
              <button className="al-del" title={a.status === "active" ? "Отменить" : "Удалить"}
                onClick={(e) => { e.stopPropagation(); delMut.mutate(a.id); }}>✕</button>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}

export default function StatusBar({ count, live, sources = {}, theme, onSetTheme }) {
  // ALOR = живой WS-поток; CBONDS — из meta (кривые ставок построены)
  const src = [
    { k: "ALOR", on: live },
    { k: "CBONDS", on: !!sources.cbonds },
  ];
  const onFloaters = useLocation().pathname.startsWith("/floaters");
  return (
    <footer className="statusbar">
      {onSetTheme && <ThemeSwitch theme={theme} onSetTheme={onSetTheme} />}
      <span className="status-cell">{live ? "ГОТОВО" : "ПОДКЛЮЧЕНИЕ"}</span>
      {onFloaters && <span className="status-cell">ИНСТРУМЕНТЫ <span className="counter">{String(count).padStart(3, "0")}</span></span>}
      <AlertsCell />
      <span className="status-cell grow" />
      {src.map((s) => (
        <span key={s.k} className={"status-cell src" + (s.on ? " on" : "")} title={s.on ? "связь активна" : "нет связи"}>
          <span className={"src-dot " + (s.on ? "on" : "off")} />{s.k}
        </span>
      ))}
    </footer>
  );
}
