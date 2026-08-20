import { useState, useRef, useMemo, useEffect } from "react";
import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchPaymentsCalendar } from "../api.js";
import { fmt } from "../format.js";
import KpisInline from "./Kpis.jsx";
import { IconRefresh } from "./icons.jsx";
import SignalsBell from "./SignalsBell.jsx";
import { usePageStatusItems } from "../pageStatus.jsx";

const mln = (v) => fmt.mln(v);   // ₽ → млн, единый формат проекта

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

// МСК-дата (UTC+3, без DST): нижняя строка живёт по торговому дню, а не по
// таймзоне ноутбука — иначе с полуночи до 03:00 «сегодня» показывало вчера
const mskISO = (shiftDays = 0) =>
  new Date(Date.now() + 3 * 3600 * 1000 + shiftDays * 864e5).toISOString().slice(0, 10);

const DAY_LABEL = { "-1": "вчера", 0: "сегодня", 1: "завтра" };

/**
 * Выплаты вокруг сегодняшнего дня: клик по дате в нижней строке открывает
 * окошко с купонами и погашениями за вчера, сегодня и завтра.
 *
 * Зачем именно тут: дата расчёта — единственное место строки, где уже написан
 * торговый день, и вопрос «кто сегодня платит» задают глядя ровно на неё.
 * Полный календарь остался вкладкой ВЫПЛАТЫ — здесь только три дня.
 */
function PaymentsCell({ calcDate, ratesDate }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const from = mskISO(-1), to = mskISO(1), today = mskISO(0);
  const q = useQuery({
    queryKey: ["payments-window", from, to],
    queryFn: () => fetchPaymentsCalendar({ from, to }),
    enabled: open,          // календарь считается по всему юниверсу — не греем зря
    staleTime: 10 * 60_000,
  });

  // группы идут в порядке вчера → сегодня → завтра, пустые дни тоже показываем:
  // «сегодня выплат нет» — это ответ, а не отсутствие ответа
  const groups = useMemo(() => {
    const ev = q.data?.events || [];
    return [-1, 0, 1].map((shift) => {
      const d = mskISO(shift);
      const rows = ev.filter((e) => e.date === d);
      return { shift, date: d, rows,
               total: rows.reduce((a, r) => a + (r.total_rub || 0), 0) };
    });
  }, [q.data]);

  const nToday = groups.find((g) => g.shift === 0)?.rows.length || 0;

  return (
    <span className={"status-cell meta-chip pay-chip" + (open ? " open" : "")} ref={box}>
      <button type="button" className="pay-btn" onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title="дата расчёта / дата ставок · клик — выплаты за вчера, сегодня и завтра">
        <span className="meta-v">{fmt.date(calcDate) || "—"}</span>
        <span className="meta-sep">/</span>
        <span className="meta-v">{fmt.date(ratesDate) || "—"}</span>
      </button>
      {open && (
        <div className="pay-pop">
          <div className="pay-pop-h">
            Выплаты · вчера — завтра
            {q.isLoading ? " · считаем…" : nToday ? ` · сегодня ${nToday}` : ""}
          </div>
          {q.isError && <div className="pay-empty">не удалось получить календарь</div>}
          {q.isLoading && <div className="pay-empty">считаем поток по юниверсу…</div>}
          {!q.isLoading && !q.isError && groups.map((g) => (
            <div key={g.shift} className={"pay-day" + (g.date === today ? " pay-today" : "")}>
              <div className="pay-day-h">
                <span>{DAY_LABEL[g.shift]} · {fmt.date(g.date)}</span>
                <span className="pay-day-sum">
                  {g.rows.length ? `${g.rows.length} · ${mln(g.total)} млн ₽` : "—"}
                </span>
              </div>
              {g.rows.length === 0
                ? <div className="pay-empty">выплат нет</div>
                : g.rows.map((r) => (
                    <div key={r.isin + r.date + r.type} className="pay-row"
                      title={`${r.emitter} · ${r.isin}`
                        + (r.projected ? " · купон не зафиксирован (проекция)" : "")}>
                      <span className="pay-nm">{r.name}</span>
                      <span className={"pay-t pay-t-" + r.type.toLowerCase()}>
                        {r.type === "COUPON" ? "купон" : "погаш"}</span>
                      <span className="pay-v">{fmt.num(r.amount_rub, 2)} ₽</span>
                      <span className="pay-v pay-tot" title="всего по выпуску, млн ₽">
                        {r.total_rub != null ? mln(r.total_rub) : "—"}</span>
                      {r.projected && <span className="pay-proj" title="проекция форвардом">≈</span>}
                    </div>
                  ))}
            </div>
          ))}
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
      {/* подписи РАСЧЁТ/СТАВКИ ушли в тултип — двух дат хватает, а строка не
          влезала. Клик по датам — выплаты за вчера/сегодня/завтра. */}
      <PaymentsCell calcDate={meta.calc_date} ratesDate={meta.rates_date} />
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
