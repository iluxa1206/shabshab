import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchBlocks, fetchBlockBoards, fetchBlockDays, fetchTapeIssuers } from "../api.js";
import { fmt, baseLabel, ratingColor } from "../format.js";
import IssuerFilter from "./IssuerFilter.jsx";

// Вкладка КРУПНЫЕ — сделки от 1 млн ₽ по ВСЕМУ рынку облигаций.
//
// Чем отличается от вкладки СДЕЛКИ: там обезличенная лента из тик-архива Alor —
// только безадресные борды и только бумаги нашего юниверса. Здесь источник ISS и
// оба рынка сразу: безадресный (TQCB/TQOB/…) и адресный ndm — РПС, РПС с ЦК,
// размещения, выкупы. Крупняк чаще всего идёт именно адресно, в стакане его не
// видно вообще.
//
// Режим «дневные РПС» — отдельная таблица: поштучных адресных сделок за прошлые
// сессии ISS не отдаёт, за те дни есть только агрегат бумага/борд/день. Поэтому
// глубже старта сбора лента поштучно пуста, а дневная — нет.

const WINDOWS = [[1, "сегодня"], [3, "3д"], [7, "7д"], [30, "30д"], [90, "90д"]];
// 0 = порог записи слоя (1 млн ₽): мельче в архиве ничего нет
const THRESHOLDS = [[0, "от 1 млн"], [5e6, "5 млн"], [1e7, "10 млн"],
                    [5e7, "50 млн"], [1e8, "100 млн"]];
const MARKETS = [[null, "все режимы"], ["bonds", "безадресные"], ["ndm", "адресные (РПС)"]];
// охват: лента шире нашего юниверса, поэтому «весь рынок» — дефолт, а срез по
// типу купона (флоатеры/фиксы) считает бэк по базе из справочников
const SCOPES = [["market", "весь рынок"], ["universe", "наш юниверс"],
                ["float", "флоатеры"], ["fixed", "фиксы"]];
const LIMITS = [500, 2000, 5000];

const money = (v) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return fmt.num(v / 1e9, 2) + " млрд";
  if (a >= 1e6) return fmt.num(v / 1e6, 1) + " млн";
  if (a >= 1e3) return fmt.num(v / 1e3, 0) + " тыс";
  return fmt.num(v, 0);
};
const ts = (s, today) => {
  if (!s) return "—";
  const [d, t] = s.split(" ");
  return d === today ? t : `${d.slice(8, 10)}.${d.slice(5, 7)} ${(t || "").slice(0, 5)}`;
};

export default function BlocksTape() {
  const nav = useNavigate();
  const [days, setDays] = useState(1);
  const [minValue, setMinValue] = useState(0);
  const [market, setMarket] = useState(null);
  const [boards, setBoards] = useState([]);       // фильтр по конкретным режимам
  const [emitters, setEmitters] = useState([]);
  const [scope, setScope] = useState("market");
  const [limit, setLimit] = useState(LIMITS[0]);
  const [pin, setPin] = useState(null);
  const [q, setQ] = useState("");
  const [view, setView] = useState("trades");     // trades | days
  const [data, setData] = useState(null);
  const [dayData, setDayData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [errMsg, setErrMsg] = useState("");
  const [issuers, setIssuers] = useState([]);
  const [boardOpts, setBoardOpts] = useState([]);
  const [tick, setTick] = useState(0);
  const abort = useRef(null);

  useEffect(() => {
    fetchTapeIssuers().then(setIssuers).catch(() => setIssuers([]));
    fetchBlockBoards(90).then(setBoardOpts).catch(() => setBoardOpts([]));
  }, []);

  useEffect(() => {
    abort.current?.abort();
    const ac = new AbortController();
    abort.current = ac;
    setStatus("loading");
    const req = view === "days"
      ? fetchBlockDays({ isin: pin, days, minValue: minValue || 1e6 }, ac.signal)
        .then((d) => { setDayData(d); })
      : fetchBlocks({ days, minValue, market, board: boards, issuer: emitters,
                      isin: pin, scope, limit }, ac.signal)
        .then((d) => { setData(d); });
    req.then(() => setStatus("ready"))
      .catch((e) => { if (e.name !== "AbortError") { setErrMsg(e.message); setStatus("error"); } });
    return () => ac.abort();
  }, [days, minValue, market, boards, emitters, pin, scope, limit, view, tick]);

  const today = useMemo(() => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }, [tick]);

  const match = (r) => {
    const s = q.trim().toLowerCase();
    if (!s) return true;
    return (r.name || "").toLowerCase().includes(s) || r.isin.toLowerCase().includes(s)
        || (r.emitter || "").toLowerCase().includes(s);
  };
  const rows = useMemo(() => (data?.blocks || []).filter(match), [data, q]);
  const dayRows = useMemo(() => (dayData?.rows || []).filter(match), [dayData, q]);

  const sum = data?.summary || {};
  const byM = sum.by_market || {};
  const toggle = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  // опции режимов сужаем выбранным рынком — иначе в списке безадресных бордов
  // висят РПС-борды, которые при market=bonds ничего не найдут
  const shownBoards = boardOpts.filter((b) => !market || b.market === market);

  return (
    <div className="issuer-agg tape-page">
      <div className="ia-head">
        <h2 className="ia-title">Крупные сделки</h2>
        <span className="ia-hint">
          сделки от 1 млн ₽ по всему рынку облигаций (шире нашего юниверса):
          безадресные режимы и адресные — РПС, РПС с ЦК, размещения, выкупы.
          Блок в адресном режиме в стакане не виден вообще
          {sum.archive_till && <> · данные до {ts(sum.archive_till, today)}</>}
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Вид">
            <button className={"seg-btn" + (view === "trades" ? " active" : "")}
              onClick={() => setView("trades")}>поштучно</button>
            <button className={"seg-btn" + (view === "days" ? " active" : "")}
              onClick={() => setView("days")} title="дневные обороты адресных режимов из ISS">
              дневные РПС
            </button>
          </span>
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
          {view === "trades" && (
            <span className="seg" role="tablist" aria-label="Режим">
              {MARKETS.map(([v, label]) => (
                <button key={label} className={"seg-btn" + (market === v ? " active" : "")}
                  onClick={() => { setMarket(v); setBoards([]); }}>{label}</button>
              ))}
            </span>
          )}
          {view === "trades" && (
            <span className="seg" role="tablist" aria-label="Охват">
              {SCOPES.map(([v, label]) => (
                <button key={v} className={"seg-btn" + (scope === v ? " active" : "")}
                  onClick={() => setScope(v)}
                  title={v === "market" ? "все облигации MOEX, шире нашего юниверса"
                    : v === "float" ? "только флоатеры (KEYRATE/RUONIA) из реестра"
                    : v === "fixed" ? "только фиксы из нашего универса"
                    : "флоатеры + фиксы нашего универса"}>{label}</button>
              ))}
            </span>
          )}
          <IssuerFilter issuers={issuers} selected={emitters}
            onToggle={(n) => setEmitters((a) => toggle(a, n))} onClear={() => setEmitters([])} />
          <input className="tape-search" placeholder="бумага / ISIN…" value={q}
            onChange={(e) => setQ(e.target.value)} />
          <button className="btn" onClick={() => setTick((t) => t + 1)}>Обновить</button>
        </div>
        {view === "trades" && shownBoards.length > 0 && (
          <div className="ia-filters blk-boards">
            <span className="ia-flabel">Режимы</span>
            {shownBoards.slice(0, 12).map((b) => (
              <button key={b.board} className={"chip-btn" + (boards.includes(b.board) ? " on" : "")}
                title={`${b.board} · ${b.n} сделок · ${money(b.value)} ₽`}
                onClick={() => setBoards((a) => toggle(a, b.board))}>{b.title}</button>
            ))}
            {boards.length > 0 && (
              <button className="btn" onClick={() => setBoards([])}>сбросить</button>
            )}
          </div>
        )}
      </div>

      {status === "error" && <div className="ia-empty">ошибка: {errMsg}</div>}
      {status === "loading" && !data && !dayData && <div className="ia-empty">читаю ленту крупных сделок…</div>}

      {view === "trades" && data && (
        <>
          <div className="tape-sum">
            <span className="tape-kpi"><span className="tape-k">СДЕЛОК</span><span className="tape-v">{fmt.num(sum.n, 0)}</span></span>
            <span className="tape-kpi"><span className="tape-k">ОБОРОТ</span><span className="tape-v">{money(sum.value)} ₽</span></span>
            <span className="tape-kpi"><span className="tape-k">БЕЗАДРЕСНЫЕ</span><span className="tape-v">{money(byM.bonds?.value)} ₽</span></span>
            <span className="tape-kpi"><span className="tape-k">АДРЕСНЫЕ</span><span className="tape-v">{money(byM.ndm?.value)} ₽</span></span>
            {(sum.top || []).length > 0 && (
              <span className="tape-top">
                <span className="tape-k">ТОП ОБОРОТА</span>
                {(sum.top || []).slice(0, 5).map((t) => (
                  <button key={t.isin} className={"chip-btn" + (pin === t.isin ? " on" : "")}
                    title={`${t.emitter || t.isin} · ${t.n} сделок`}
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
                  <th className="left">РЕЖИМ</th>
                  <th>ЦЕНА, %</th>
                  <th>ОБЪЁМ, шт</th>
                  <th>СУММА</th>
                  <th>ДОХ-ТЬ, %</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  // график выпуска построен вокруг флоатера — для фиксов/ОФЗ и
                  // бумаг вне юниверса он пустой, такие строки никуда не ведут
                  const clickable = r.base === "KEYRATE" || r.base === "RUONIA";
                  return (
                    <tr key={r.trade_id}
                      className={(clickable ? "" : "tape-row-static ")
                                 + (r.negotiated ? "blk-ndm" : "")}
                      onClick={clickable ? () => nav(`/chart/${r.isin}`) : undefined}
                      title={`${r.isin} · ${r.ts} · ${r.board}`}>
                      <td className="left tape-ts">{ts(r.ts, today)}</td>
                      <td className="left tape-name">
                        {r.name}
                        {r.rating && <span className="tape-rt" style={{ color: ratingColor(r.rating) }}>{r.rating}</span>}
                        {r.base && <span className="tape-base">{r.base === "FIXED" ? "фикс" : baseLabel(r.base)}</span>}
                      </td>
                      <td className="left tape-emitter">{r.emitter || "—"}</td>
                      <td className="left blk-board">
                        <span className={r.negotiated ? "blk-tag blk-tag-ndm" : "blk-tag"}>
                          {r.board_title || r.board}
                        </span>
                      </td>
                      <td className="num">{fmt.pct(r.price)}</td>
                      <td className="num">{fmt.num(r.qty, 0)}</td>
                      <td className="num tape-val">
                        {money(r.value)}{r.cur && r.cur !== "SUR" ? ` ${r.cur}` : " ₽"}
                      </td>
                      <td className="num">{r.yld != null ? fmt.num(r.yld, 2) : "—"}</td>
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

      {view === "days" && dayData && (
        <>
          <div className="tape-sum">
            <span className="ia-hint">
              дневные обороты адресных режимов из ISS: поштучных РПС-сделок за прошлые
              сессии биржа не отдаёт, поэтому за дни до запуска сбора это единственный
              след блока — бумага, режим, оборот и средневзвешенная цена дня
            </span>
          </div>
          <div className="ia-table-wrap">
            <table className="grid tape-table">
              <thead>
                <tr>
                  <th className="left">ДАТА</th>
                  <th className="left">БУМАГА</th>
                  <th className="left">ЭМИТЕНТ</th>
                  <th className="left">РЕЖИМ</th>
                  <th>СДЕЛОК</th>
                  <th>СРЕДНЕВЗВЕС, %</th>
                  <th>ОБЪЁМ, шт</th>
                  <th>ОБОРОТ, ₽</th>
                </tr>
              </thead>
              <tbody>
                {dayRows.map((r) => (
                  <tr key={r.isin + r.date + r.board} title={r.isin}>
                    <td className="left tape-ts">{fmt.date(r.date)}</td>
                    <td className="left tape-name">{r.name}</td>
                    <td className="left tape-emitter">{r.emitter || "—"}</td>
                    <td className="left blk-board">
                      <span className="blk-tag blk-tag-ndm">{r.board_title || r.board}</span>
                    </td>
                    <td className="num">{fmt.num(r.numtrades, 0)}</td>
                    <td className="num">{r.waprice != null ? fmt.pct(r.waprice) : "—"}</td>
                    <td className="num">{fmt.num(r.volume, 0)}</td>
                    <td className="num tape-val">{money(r.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {dayRows.length === 0 && status === "ready" && <div className="ia-empty">нет дневных оборотов под фильтром</div>}
          </div>
        </>
      )}
    </div>
  );
}
