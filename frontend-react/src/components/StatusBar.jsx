import { useState, useRef, useMemo, useCallback, useEffect } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchAlerts, deleteAlert } from "../api.js";
import { fmt } from "../format.js";
import KpisInline from "./Kpis.jsx";
import { IconBell, IconAlert, IconRefresh } from "./icons.jsx";
import SignalsBell from "./SignalsBell.jsx";

const mln = (v) => (v != null ? (v / 1e6).toFixed(1) : null);   // ₽ → млн

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

// Плашка алертов: активные + сработавшие. При fired — красная. Hover → список.
function AlertsCell({ bonds = [] }) {
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

  // isin → строка бумаги (имя/цена/оборот/DM) для обогащения попапа
  const byIsin = useMemo(() => {
    const m = {};
    for (const b of bonds) if (b && b.isin) m[b.isin] = b;
    return m;
  }, [bonds]);

  // hover-intent: закрытие с задержкой — курсор может на миг выйти за плашку/
  // попасть в микрозазор, попап не должен мигать. Вход отменяет закрытие.
  const closeT = useRef(null);
  const cancelClose = useCallback(() => {
    if (closeT.current) { clearTimeout(closeT.current); closeT.current = null; }
  }, []);
  const openNow = useCallback(() => { cancelClose(); setOpen(true); }, [cancelClose]);
  const closeSoon = useCallback(() => {
    cancelClose();
    closeT.current = setTimeout(() => setOpen(false), 260);
  }, [cancelClose]);

  // клик по алерту → карточка + стакан (ob=1; стакан и так открыт по умолчанию,
  // параметр держим явным — он перебивает опт-аут ?ob=0, если тот в адресе)
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
    <span className={"status-cell al-chip" + (open ? " open" : "")}
      onMouseEnter={openNow} onMouseLeave={closeSoon}>
      <span className={"al-chip-lbl" + (hasFired ? " fired" : "")}>
        <IconBell /> {active.length}{hasFired && <span className="al-chip-fired"> · {fired.length} <IconAlert /></span>}
      </span>
      {open && (
        <div className="al-pop" onMouseEnter={cancelClose} onMouseLeave={closeSoon}>
          <div className="al-pop-h">Алерты · активных {active.length}{hasFired && `, сработало ${fired.length}`}</div>
          {shown.map((a) => {
            const b = byIsin[a.isin] || {};
            const px = b.last_price_pct != null ? fmt.pct(b.last_price_pct) : null;
            const vol = mln(b.val_today);
            const dm = b.disc_margin_bps ?? b.dm_bps;
            return (
              <div key={a.id} className={"al-pop-row" + (a.status === "fired" ? " fired" : "")}
                onClick={() => openBond(a)} title="Открыть карточку и стакан">
                <span className={"al-side al-" + a.side}>{a.side === "buy" ? "покуп" : "прод"}</span>
                <span className="al-pop-name" title={a.isin}>{b.short_name || a.isin}</span>
                <span className="al-pop-cond">{a.metric} {a.op} {fmt.num(a.threshold, 2)}</span>
                <span className="al-pop-m" title="цена, чистая %">{px != null ? `${px}%` : "—"}</span>
                <span className="al-pop-m" title="оборот сегодня, млн ₽">{vol != null ? `${vol}` : "—"}</span>
                <span className="al-pop-m" title="DM, б.п.">{dm != null ? `${dm}` : "—"}</span>
                <span className="al-pop-st">{a.status === "fired" ? <><IconAlert /> {fmt.pct(a.fired_price)}%</> : "ждёт"}</span>
                <button className="al-del" title={a.status === "active" ? "Отменить" : "Удалить"}
                  onClick={(e) => { e.stopPropagation(); delMut.mutate(a.id); }}>✕</button>
              </div>
            );
          })}
        </div>
      )}
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

export default function StatusBar({ count, bonds = [], kpiBonds = [], live, sources = {}, theme, onSetTheme,
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
      <AlertsCell bonds={bonds} />
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
