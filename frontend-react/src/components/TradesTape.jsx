import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchMarketTape, fetchTapeIssuers } from "../api.js";
import { fmt, baseLabel, ratingColor } from "../format.js";
import IssuerFilter from "./IssuerFilter.jsx";

// Вкладка СДЕЛКИ — общерыночная лента обезличенных сделок из тикового архива.
// Данные наливает часовой демон, поэтому хвост ленты отстаёт максимум на прогон:
// «данные до …» в шапке показывает реальную свежесть, не время открытия страницы.

const WINDOWS = [[1, "сегодня"], [3, "3д"], [7, "7д"], [30, "30д"]];
const THRESHOLDS = [[0, "все"], [1e6, "1 млн"], [1e7, "10 млн"], [5e7, "50 млн"]];
const SIDES = [[null, "все"], ["buy", "покупка"], ["sell", "продажа"]];
const LIMITS = [500, 2000, 5000];

const money = (v) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return fmt.num(v / 1e9, 2) + " млрд";
  if (a >= 1e6) return fmt.num(v / 1e6, 1) + " млн";
  if (a >= 1e3) return fmt.num(v / 1e3, 0) + " тыс";
  return fmt.num(v, 0);
};
// '2026-08-04 12:07:59' → '12:07:59' сегодня, иначе '04.08 12:07'
const ts = (s, today) => {
  if (!s) return "—";
  const [d, t] = s.split(" ");
  return d === today ? t : `${d.slice(8, 10)}.${d.slice(5, 7)} ${(t || "").slice(0, 5)}`;
};

function SideTag({ side }) {
  if (side !== "buy" && side !== "sell") return <span className="tape-side">—</span>;
  const buy = side === "buy";
  return <span className={"tape-side " + (buy ? "tape-buy" : "tape-sell")}>{buy ? "покупка" : "продажа"}</span>;
}

export default function TradesTape() {
  const nav = useNavigate();
  const [days, setDays] = useState(1);
  const [minValue, setMinValue] = useState(1e6);
  const [side, setSide] = useState(null);
  const [limit, setLimit] = useState(LIMITS[0]);
  const [emitters, setEmitters] = useState([]);
  const [pin, setPin] = useState(null);         // одна бумага (клик по топу оборота)
  const [q, setQ] = useState("");
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [errMsg, setErrMsg] = useState("");
  const [issuers, setIssuers] = useState([]);
  const [tick, setTick] = useState(0);          // ручное обновление
  const abort = useRef(null);

  useEffect(() => {
    fetchTapeIssuers().then(setIssuers).catch(() => setIssuers([]));
  }, []);

  useEffect(() => {
    abort.current?.abort();
    const ac = new AbortController();
    abort.current = ac;
    setStatus("loading");
    fetchMarketTape({ days, minValue, side, issuer: emitters, isin: pin, limit }, ac.signal)
      .then((d) => { setData(d); setStatus("ready"); })
      .catch((e) => { if (e.name !== "AbortError") { setErrMsg(e.message); setStatus("error"); } });
    return () => ac.abort();
  }, [days, minValue, side, limit, emitters, pin, tick]);

  const today = useMemo(() => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }, [tick]);

  const rows = useMemo(() => {
    const s = q.trim().toLowerCase();
    const all = data?.trades || [];
    return s ? all.filter((r) => (r.name || "").toLowerCase().includes(s)
                              || r.isin.toLowerCase().includes(s)
                              || (r.emitter || "").toLowerCase().includes(s)) : all;
  }, [data, q]);

  const sum = data?.summary || {};
  const toggleEmitter = (name) =>
    setEmitters((a) => (a.includes(name) ? a.filter((x) => x !== name) : [...a, name]));

  return (
    <div className="issuer-agg tape-page">
      <div className="ia-head">
        <h2 className="ia-title">Лента сделок</h2>
        <span className="ia-hint">
          обезличенные сделки по всему юниверсу: цена, объём, рублёвая сумма и агрессор
          (сторона, которая забрала заявку). Архив наливается часовым демоном
          {sum.archive_till && <> · данные до {ts(sum.archive_till, today)} {sum.archive_till.slice(0, 10) !== today && `(${fmt.date(sum.archive_till.slice(0, 10))})`}</>}
          {days > 30 && <> · глубже 35 дней в архиве остаются только сделки от 1 млн ₽</>}
        </span>
        <div className="ia-filters">
          <span className="ia-flabel">Окно</span>
          <span className="seg" role="tablist" aria-label="Окно">
            {WINDOWS.map(([d, label]) => (
              <button key={d} className={"seg-btn" + (days === d ? " active" : "")}
                onClick={() => setDays(d)}>{label}</button>
            ))}
          </span>
          <span className="ia-flabel">От</span>
          <span className="seg" role="tablist" aria-label="Порог суммы">
            {THRESHOLDS.map(([v, label]) => (
              <button key={v} className={"seg-btn" + (minValue === v ? " active" : "")}
                onClick={() => setMinValue(v)}>{label}</button>
            ))}
          </span>
          <span className="seg" role="tablist" aria-label="Сторона">
            {SIDES.map(([v, label]) => (
              <button key={label} className={"seg-btn" + (side === v ? " active" : "")}
                onClick={() => setSide(v)}>{label}</button>
            ))}
          </span>
          <IssuerFilter issuers={issuers} selected={emitters}
            onToggle={toggleEmitter} onClear={() => setEmitters([])} />
          <input className="tape-search" placeholder="бумага / ISIN…" value={q}
            onChange={(e) => setQ(e.target.value)} />
          <button className="btn" onClick={() => setTick((t) => t + 1)}>Обновить</button>
        </div>
      </div>

      {status === "error" && <div className="ia-empty">ошибка: {errMsg}</div>}
      {status === "loading" && !data && <div className="ia-empty">читаю архив сделок…</div>}

      {data && (
        <>
          <div className="tape-sum">
            <span className="tape-kpi"><span className="tape-k">СДЕЛОК</span><span className="tape-v">{fmt.num(sum.n, 0)}</span></span>
            <span className="tape-kpi"><span className="tape-k">ОБОРОТ</span><span className="tape-v">{money(sum.value)} ₽</span></span>
            <span className="tape-kpi"><span className="tape-k">ПОКУПКИ</span><span className="tape-v tape-buy">{money(sum.buy_value)} ₽</span></span>
            <span className="tape-kpi"><span className="tape-k">ПРОДАЖИ</span><span className="tape-v tape-sell">{money(sum.sell_value)} ₽</span></span>
            {(sum.issuers_top || []).length > 0 && (
              <span className="tape-top">
                <span className="tape-k">ТОП ОБОРОТА</span>
                {(sum.issuers_top || []).slice(0, 5).map((t) => (
                  <button key={t.isin} className={"chip-btn" + (pin === t.isin ? " on" : "")}
                    title={`${t.emitter || ""} · ${t.n} сделок`}
                    onClick={() => setPin((p) => (p === t.isin ? null : t.isin))}>
                    {t.name} {money(t.value)}
                  </button>
                ))}
              </span>
            )}
          </div>

          <div className="ia-table-wrap">
            <table className="grid tape-table">
              <thead>
                <tr>
                  <th className="left">ВРЕМЯ</th>
                  <th className="left">БУМАГА</th>
                  <th className="left">ЭМИТЕНТ</th>
                  <th>ЦЕНА, %</th>
                  <th>ОБЪЁМ, шт</th>
                  <th>СУММА, ₽</th>
                  <th className="left">СТОРОНА</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  // график выпуска построен вокруг флоатера (Y-IDX, спред-слои);
                  // для фиксов/ОФЗ он пустой — такие строки не ведут никуда
                  const clickable = r.base === "KEYRATE" || r.base === "RUONIA";
                  return (
                  <tr key={r.isin + "/" + r.trade_id}
                    className={clickable ? "" : "tape-row-static"}
                    onClick={clickable ? () => nav(`/chart/${r.isin}`) : undefined}
                    title={`${r.isin} · ${r.ts}`}>
                    <td className="left tape-ts">{ts(r.ts, today)}</td>
                    <td className="left tape-name">
                      {r.name}
                      {r.rating && <span className="tape-rt" style={{ color: ratingColor(r.rating) }}>{r.rating}</span>}
                      {r.base && <span className="tape-base">{r.base === "FIXED" ? "фикс" : baseLabel(r.base)}</span>}
                    </td>
                    <td className="left tape-emitter">{r.emitter || "—"}</td>
                    <td className="num">{fmt.pct(r.price)}</td>
                    <td className="num">{fmt.num(r.qty, 0)}</td>
                    <td className="num tape-val">{money(r.value)}</td>
                    <td className="left"><SideTag side={r.side} /></td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
            {rows.length === 0 && status === "ready" && <div className="ia-empty">нет сделок под фильтром</div>}
          </div>

          {data.truncated && (
            <div className="tape-more">
              показаны последние {fmt.num(rows.length, 0)} из {fmt.num(sum.n, 0)}
              {LIMITS.filter((l) => l > limit).slice(0, 1).map((l) => (
                <button key={l} className="btn" onClick={() => setLimit(l)}>показать {fmt.num(l, 0)}</button>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
