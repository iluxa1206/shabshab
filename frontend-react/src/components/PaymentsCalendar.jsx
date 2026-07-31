import { useEffect, useMemo, useState } from "react";
import { fetchPaymentsCalendar } from "../api.js";
import { fmt } from "../format.js";
import IssuerFilter from "./IssuerFilter.jsx";

// Календарь выплат юниверса флоатеров: купоны + погашения в ₽ на одну бумагу.
// Два режима: сетка-календарь месяца (ховер-попап по дню) и строки (лента по датам).

const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const WDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

// локальная дата → ISO без TZ-сдвига (toISOString уехал бы на день в UTC)
const iso = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};
const rub = (v) => fmt.num(v, 2) + " ₽";
// объём по выпуску: компактно (тыс/млн/млрд ₽)
const vol = (v) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return fmt.num(v / 1e9, 2) + " млрд ₽";
  if (a >= 1e6) return fmt.num(v / 1e6, 1) + " млн ₽";
  if (a >= 1e3) return fmt.num(v / 1e3, 0) + " тыс ₽";
  return fmt.num(v, 0) + " ₽";
};

function TypeBadge({ type }) {
  const cpn = type === "COUPON";
  return <span className={"pay-badge " + (cpn ? "pay-cpn" : "pay-red")}>{cpn ? "купон" : "погаш."}</span>;
}

// строки одного дня (общие для попапа и режима «Строки»)
function DayRows({ events, compact }) {
  return (
    <table className={"grid pay-day-table" + (compact ? " compact" : "")}>
      <tbody>
        {events.map((e, i) => (
          <tr key={e.isin + e.type + i}>
            <td className="left pay-name" title={e.isin}>{e.name}</td>
            <td className="left pay-emitter">{e.emitter}</td>
            <td className="left"><TypeBadge type={e.type} /></td>
            <td className="num pay-rate">
              {e.rate_pct != null ? (e.projected ? "~" : "") + fmt.pct(e.rate_pct) + "%" : "—"}
            </td>
            <td className="num pay-amt">{rub(e.amount_rub)}</td>
            <td className="num pay-vol">{vol(e.total_rub)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DaySummary({ events }) {
  const cpn = events.filter((e) => e.type === "COUPON");
  const red = events.filter((e) => e.type === "REDEMPTION");
  // Σ дня — объём держателям всего (по выпускам с известным ISSUESIZE)
  const total = events.reduce((s, e) => s + (e.total_rub || 0), 0);
  return (
    <div className="pay-sum">
      {cpn.length > 0 && <span className="pay-chip pay-cpn">купоны {cpn.length}</span>}
      {red.length > 0 && <span className="pay-chip pay-red">погаш. {red.length}</span>}
      {total > 0 && <span className="pay-total">Σ {vol(total)}</span>}
    </div>
  );
}

function MonthGrid({ cursor, byDate }) {
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
  const daysIn = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0).getDate();
  const lead = (first.getDay() + 6) % 7;   // Пн=0
  const todayIso = iso(new Date());
  const cells = [];
  for (let i = 0; i < lead; i++) cells.push(null);
  for (let d = 1; d <= daysIn; d++)
    cells.push(new Date(cursor.getFullYear(), cursor.getMonth(), d));
  while (cells.length % 7) cells.push(null);

  const weeks = Math.ceil(cells.length / 7);
  return (
    <div className="cal-grid" style={{ "--cal-weeks": weeks }}>
      {WDAYS.map((w) => <div key={w} className="cal-wday">{w}</div>)}
      {cells.map((d, i) => {
        if (!d) return <div key={"e" + i} className="cal-cell cal-empty" />;
        const k = iso(d);
        const evs = byDate.get(k) || [];
        const isToday = k === todayIso;
        const wknd = i % 7 >= 5;
        // попап вниз для верхних 2 недель, вверх для нижних; вправо/влево по колонке
        const popCls =
          (Math.floor(i / 7) <= 2 ? " pop-down" : " pop-up") +
          (i % 7 >= 4 ? " pop-left" : "");
        return (
          <div key={k} className={"cal-cell" + (isToday ? " cal-today" : "") + (wknd ? " cal-wknd" : "") + (evs.length ? " cal-has" : "")}>
            <div className="cal-daynum">{d.getDate()}</div>
            {evs.length > 0 && (
              <>
                <DaySummary events={evs} />
                <div className={"cal-pop" + popCls}>
                  <div className="cal-pop-head">{fmt.date(k)} · {evs.length} выпл.</div>
                  <DayRows events={evs.slice(0, 40)} compact />
                  {evs.length > 40 && <div className="cal-pop-more">…ещё {evs.length - 40}</div>}
                </div>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

function RowsView({ byDate }) {
  const dates = [...byDate.keys()].sort();
  if (!dates.length) return <div className="ia-empty">нет выплат под фильтром</div>;
  let lastMonth = null;
  return (
    <div className="pay-rows">
      {dates.map((k) => {
        const d = new Date(k + "T00:00:00");
        const mkey = k.slice(0, 7);
        const showMonth = mkey !== lastMonth;
        lastMonth = mkey;
        const evs = byDate.get(k);
        return (
          <div key={k}>
            {showMonth && <div className="pay-month-head">{MONTHS[d.getMonth()]} {d.getFullYear()}</div>}
            <div className="pay-date-group">
              <div className="pay-date-head">
                <span className="pay-date">{fmt.date(k)}</span>
                <span className="pay-wday">{WDAYS[(d.getDay() + 6) % 7]}</span>
                <DaySummary events={evs} />
              </div>
              <DayRows events={evs} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function PaymentsCalendar() {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [errMsg, setErrMsg] = useState("");
  const [mode, setMode] = useState(() => localStorage.getItem("payMode") || "cal"); // cal | rows
  const [typesSel, setTypesSel] = useState([]);   // COUPON / REDEMPTION (пусто = все)
  const [emittersSel, setEmittersSel] = useState([]);
  const [cursor, setCursor] = useState(() => { const t = new Date(); return new Date(t.getFullYear(), t.getMonth(), 1); });

  useEffect(() => { localStorage.setItem("payMode", mode); }, [mode]);

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    fetchPaymentsCalendar()
      .then((d) => { if (alive) { setData(d); setStatus("ready"); } })
      .catch((e) => { if (alive) { setErrMsg(e.message); setStatus("error"); } });
    return () => { alive = false; };
  }, []);

  const events = data?.events || [];

  const issuers = useMemo(() => {
    const m = new Map();
    for (const e of events) {
      if (!m.has(e.emitter)) m.set(e.emitter, new Set());
      m.get(e.emitter).add(e.isin);
    }
    return [...m.entries()].map(([name, s]) => ({ name, count: s.size }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [events]);

  const filtered = useMemo(() => {
    let rows = events;
    if (typesSel.length) rows = rows.filter((e) => typesSel.includes(e.type));
    if (emittersSel.length) rows = rows.filter((e) => emittersSel.includes(e.emitter));
    return rows;
  }, [events, typesSel, emittersSel]);

  const byDate = useMemo(() => {
    const m = new Map();
    for (const e of filtered) {
      if (!m.has(e.date)) m.set(e.date, []);
      m.get(e.date).push(e);
    }
    for (const arr of m.values())
      arr.sort((a, b) => a.emitter.localeCompare(b.emitter) || a.name.localeCompare(b.name));
    return m;
  }, [filtered]);

  const toggle = (setter) => (v) =>
    setter((a) => (a.includes(v) ? a.filter((x) => x !== v) : [...a, v]));

  const shift = (n) => setCursor((c) => new Date(c.getFullYear(), c.getMonth() + n, 1));

  return (
    <div className="issuer-agg pay-cal">
      <div className="ia-head">
        <h2 className="ia-title">Календарь выплат</h2>
        <span className="ia-hint">
          купоны и погашения: ₽ на бумагу и объём держателям всего по выпуску (× штук в обращении); «~» — купон не зафиксирован, проекция форвардом; Σ дня — суммарный объём
          {data && <> · расчёт {fmt.date(String(data.calc_date))} · окно до {fmt.date(String(data.date_to))}</>}
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Режим">
            <button className={"seg-btn" + (mode === "cal" ? " active" : "")} onClick={() => setMode("cal")}>Календарь</button>
            <button className={"seg-btn" + (mode === "rows" ? " active" : "")} onClick={() => setMode("rows")}>Строки</button>
          </span>
          <button className={"chip-btn" + (typesSel.includes("COUPON") ? " on" : "")}
            onClick={() => toggle(setTypesSel)("COUPON")}>Купоны</button>
          <button className={"chip-btn" + (typesSel.includes("REDEMPTION") ? " on" : "")}
            onClick={() => toggle(setTypesSel)("REDEMPTION")}>Погашения</button>
          <IssuerFilter issuers={issuers} selected={emittersSel}
            onToggle={toggle(setEmittersSel)} onClear={() => setEmittersSel([])} />
          {mode === "cal" && (
            <span className="pay-monthnav">
              <button className="btn" onClick={() => shift(-1)} aria-label="Пред. месяц">‹</button>
              <span className="pay-month-label">{MONTHS[cursor.getMonth()]} {cursor.getFullYear()}</span>
              <button className="btn" onClick={() => shift(1)} aria-label="След. месяц">›</button>
              <button className="btn" onClick={() => { const t = new Date(); setCursor(new Date(t.getFullYear(), t.getMonth(), 1)); }}>сегодня</button>
            </span>
          )}
        </div>
      </div>

      {status === "loading" && <div className="ia-empty">считаю календарь по юниверсу…</div>}
      {status === "error" && <div className="ia-empty">ошибка: {errMsg}</div>}
      {status === "ready" && (mode === "cal"
        ? <MonthGrid cursor={cursor} byDate={byDate} />
        : <RowsView byDate={byDate} />)}
    </div>
  );
}
