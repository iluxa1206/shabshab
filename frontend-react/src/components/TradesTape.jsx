import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchMarketTape, fetchBlockDays, fetchTapeIssuers, fetchTapeRatings } from "../api.js";
import { fmt, baseLabel, ratingColor, dmColor } from "../format.js";
import IssuerFilter from "./IssuerFilter.jsx";

// Вкладка СДЕЛКИ — единая лента рынка.
//
// Под капотом два архива, но для пользователя это одна лента (склейка по
// TRADENO и её правила — services/tape):
//   • тиковый архив Alor: все безадресные сделки любого размера, но только по
//     нашему юниверсу;
//   • крупные сделки всего рынка из ISS: от 1 млн ₽, зато включая адресные
//     режимы — РПС, РПС с ЦК, размещения, выкупы, которых в стакане нет вообще.
//
// Режим РПС — такая же поштучная лента, только адресных сделок. Кнопка «по
// дням» переключает её на дневной агрегат бумага/борд/день: поштучных адресных
// сделок за прошлые сессии ISS не отдаёт вообще, и за дни до старта нашего
// сбора агрегат — единственный след блока.

const WINDOWS = [[1, "сегодня"], [7, "7д"], [30, "30д"], [90, "90д"],
                 [180, "180д"], [400, "макс"]];
// 0 = без порога: мельче 1 млн ₽ сделки есть только по юниверсу (тик-архив)
const THRESHOLDS = [[0, "все"], [1e6, "1"], [5e6, "5"], [1e7, "10"],
                    [5e7, "50"], [1e8, "100"]];
// РПС здесь = дневной агрегат адресных режимов (см. шапку файла)
const MARKETS = [[null, "все"], ["bonds", "Т+"], ["ndm", "РПС"]];
const SIDES = [[null, "любая"], ["buy", "buy"], ["sell", "sell"]];
// Охват. Дефолт — флоатеры: стол про них, а крупняк рынка в рублёвом объёме
// это почти целиком ОФЗ-ПД, и без фильтра лента вырождалась в ленту фиксов.
// «Весь рынок» остаётся как контекст (там же фиксы и бумаги вне реестра).
const SCOPES = [["float", "флоатеры"], ["market", "весь рынок"]];
const LIMITS = [5000, 10000, 20000];
// Такт автообновления. Безадресные сделки юниверса приходят тиком Alor почти
// сразу, адресные — из ISS с её задержкой ~15 мин, так что чаще смысла нет:
// запрос стоит агрегата по всему окну на стороне бэка.
const LIVE_MS = 20000;
const ISIN_RE = /^[A-Z]{2}[A-Z0-9]{9}\d$/;

// Деньги везде в проекте — в МЛН ₽ голым числом (fmt.mln): единица подписана
// один раз в шапке колонки. Прежний авто-масштаб (900 ₽ / 45 тыс / 1,05 млрд)
// в одной колонке был несравним глазами.
const money = (v) => (v == null ? "—" : fmt.mln(v));
const dpart = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}` : "—");
const tpart = (s) => ((s || "").split(" ")[1] || "").slice(0, 8) || "—";
const num = (s) => (s === "" || s == null ? null : Number(s));

function SideTag({ side }) {
  if (side !== "buy" && side !== "sell") return <span className="tape-side">—</span>;
  const buy = side === "buy";
  return <span className={"tape-side " + (buy ? "tape-buy" : "tape-sell")}>
    {buy ? "buy" : "sell"}</span>;
}

export default function TradesTape() {
  const nav = useNavigate();
  // дефолт — рабочий срез: неделя и крупняк от 1 млн ₽. Максимум («макс» +
  // «все») лента тянет, но агрегат по миллиону сделок считается секундами.
  const [days, setDays] = useState(7);
  const [minValue, setMinValue] = useState(1e6);
  const [side, setSide] = useState(null);
  const [market, setMarket] = useState(null);
  const [emitters, setEmitters] = useState([]);
  const [scope, setScope] = useState("float");
  const [limit, setLimit] = useState(LIMITS[0]);
  const [spreadMin, setSpreadMin] = useState("");
  const [spreadMax, setSpreadMax] = useState("");
  const [ttmMin, setTtmMin] = useState("");
  const [ttmMax, setTtmMax] = useState("");
  const [ratings, setRatings] = useState([]);       // выбранные грейды
  const [ratingOpts, setRatingOpts] = useState([]);
  const [pin, setPin] = useState(null);
  // «по дням» — агрегат бумага/режим/день вместо поштучной ленты. Только для
  // адресных: у безадресных поштучный архив полный, агрегировать нечего.
  const [byDay, setByDay] = useState(false);
  const [q, setQ] = useState("");
  const [data, setData] = useState(null);
  const [dayData, setDayData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [errMsg, setErrMsg] = useState("");
  const [issuers, setIssuers] = useState([]);
  const [tick, setTick] = useState(0);
  const [live, setLive] = useState(true);
  const [lastAt, setLastAt] = useState(null);
  const abort = useRef(null);

  // ISIN в поиске — не текстовый фильтр по загруженным строкам, а сужение
  // запроса: тогда и лента, и итоги считаются бэком ПО ЭТОЙ бумаге целиком,
  // а не по обрезанному лимитом куску
  const qIsin = useMemo(() => {
    const s = q.trim().toUpperCase();
    return ISIN_RE.test(s) ? s : null;
  }, [q]);
  const isinReq = pin || qIsin;
  const daysView = market === "ndm" && byDay;

  useEffect(() => {
    fetchTapeIssuers().then(setIssuers).catch(() => setIssuers([]));
    fetchTapeRatings().then(setRatingOpts).catch(() => setRatingOpts([]));
  }, []);

  useEffect(() => {
    abort.current?.abort();
    const ac = new AbortController();
    abort.current = ac;
    setStatus("loading");
    const req = daysView
      ? fetchBlockDays({ isin: isinReq, days, minValue: minValue || 1e6,
                         scope, issuer: emitters, ttmMin: num(ttmMin),
                         ttmMax: num(ttmMax), rating: ratings, limit }, ac.signal)
        .then((d) => { setDayData(d); })
      : fetchMarketTape({ days, minValue, side, market, issuer: emitters,
                          isin: isinReq, scope, limit, spreadMin: num(spreadMin),
                          spreadMax: num(spreadMax), ttmMin: num(ttmMin),
                          ttmMax: num(ttmMax), rating: ratings }, ac.signal)
        .then((d) => { setData(d); });
    req.then(() => { setStatus("ready"); setLastAt(new Date()); })
      .catch((e) => { if (e.name !== "AbortError") { setErrMsg(e.message); setStatus("error"); } });
    return () => ac.abort();
  }, [days, minValue, side, market, daysView, emitters, isinReq, scope, limit,
      spreadMin, spreadMax, ttmMin, ttmMax, ratings, tick]);

  // Лайв: лента дотягивается сама. Опрос, а не WS — сделки приезжают фоновыми
  // демонами (тик Alor и лента ISS), поэтому в сокете не было бы ничего, чего
  // не даст такт опроса, а запрос ленты уже умеет фильтры и агрегат.
  // Во вкладке в фоне не опрашиваем: незачем жечь агрегат по всему окну.
  useEffect(() => {
    if (!live) return undefined;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") setTick((t) => t + 1);
    }, LIVE_MS);
    const onShow = () => { if (document.visibilityState === "visible") setTick((t) => t + 1); };
    document.addEventListener("visibilitychange", onShow);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", onShow); };
  }, [live]);

  // текстовый поиск (не ISIN) — фильтр по уже загруженным строкам
  const match = (r) => {
    const s = qIsin ? "" : q.trim().toLowerCase();
    if (!s) return true;
    return (r.name || "").toLowerCase().includes(s) || r.isin.toLowerCase().includes(s)
        || (r.emitter || "").toLowerCase().includes(s);
  };
  const rows = useMemo(() => (data?.trades || []).filter(match), [data, q, qIsin]);
  const dayRows = useMemo(() => (dayData?.rows || []).filter(match), [dayData, q, qIsin]);

  // Итоги. Пока фильтр серверный (ISIN/эмитент/спред/срок) — берём агрегат бэка:
  // он посчитан по ВСЕМ сделкам окна, а не по срезанным лимитом. Как только
  // включается локальный текстовый поиск — считаем по видимым строкам, иначе
  // цифры не соответствовали бы таблице.
  const local = !qIsin && q.trim() !== "";
  const sum = useMemo(() => {
    if (!local) return data?.summary || {};
    let n = 0, value = 0, buy = 0, sell = 0, ndm = 0;
    for (const r of rows) {
      n += 1;
      const v = (!r.cur || r.cur === "SUR") ? (r.value || 0) : 0;
      value += v;
      if (r.side === "buy") buy += r.value || 0;
      if (r.side === "sell") sell += r.value || 0;
      if (r.negotiated) ndm += r.value || 0;
    }
    return { n, value, buy_value: buy, sell_value: sell,
             by_market: { ndm: { value: ndm } }, top: data?.summary?.top || [],
             archive_till: data?.summary?.archive_till, partial: true };
  }, [local, rows, data]);
  const byM = sum.by_market || {};

  const daySum = useMemo(() => {
    let n = 0, value = 0, trades = 0;
    for (const r of dayRows) { n += 1; value += r.value || 0; trades += r.numtrades || 0; }
    return { n, value, trades };
  }, [dayRows]);

  const toggle = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  return (
    <div className="issuer-agg tape-page">
      <div className="ia-head">
        <h2 className="ia-title">Лента сделок</h2>
        <div className="ia-filters">
          <span className="ia-flabel">Окно</span>
          <span className="seg" role="tablist" aria-label="Окно">
            {WINDOWS.map(([d, label]) => (
              <button key={d} className={"seg-btn" + (days === d ? " active" : "")}
                onClick={() => setDays(d)}>{label}</button>
            ))}
          </span>
          <span className="ia-flabel">От, млн ₽</span>
          <span className="seg" role="tablist" aria-label="Порог суммы">
            {THRESHOLDS.map(([v, label]) => (
              <button key={v} className={"seg-btn" + (minValue === v ? " active" : "")}
                onClick={() => setMinValue(v)}>{label}</button>
            ))}
          </span>
          <span className="seg" role="tablist" aria-label="Режим">
            {MARKETS.map(([v, label]) => (
              <button key={label} className={"seg-btn" + (market === v ? " active" : "")}
                onClick={() => setMarket(v)}
                title={v === "ndm" ? "адресные режимы: РПС, РПС с ЦК, размещения, выкупы"
                  : v === "bonds" ? "безадресный стакан" : "все режимы"}>{label}</button>
            ))}
          </span>
          {market === "ndm" && (
            <button className={"chip-btn" + (byDay ? " on" : "")}
              onClick={() => setByDay((v) => !v)}
              title="агрегат бумага/режим/день из ISS. За дни до старта поштучного сбора это ЕДИНСТВЕННЫЙ след адресных сделок — поштучно биржа их за прошлые сессии не отдаёт">
              по дням
            </button>
          )}
          {!daysView && (
            <span className="seg" role="tablist" aria-label="Сторона">
              {SIDES.map(([v, label]) => (
                <button key={label} className={"seg-btn" + (side === v ? " active" : "")}
                  onClick={() => setSide(v)}
                  title="агрессор — сторона, забравшая заявку; у адресных сделок его нет">
                  {label}</button>
              ))}
            </span>
          )}
          <span className="seg" role="tablist" aria-label="Охват">
            {SCOPES.map(([v, label]) => (
              <button key={v} className={"seg-btn" + (scope === v ? " active" : "")}
                onClick={() => setScope(v)}
                title={v === "float" ? "только флоатеры (KEYRATE/RUONIA) из реестра"
                  : "все облигации MOEX, включая фиксы и бумаги вне реестра"}>{label}</button>
            ))}
          </span>
          <IssuerFilter issuers={issuers} selected={emitters}
            onToggle={(n) => setEmitters((a) => toggle(a, n))} onClear={() => setEmitters([])} />
          <input className="tape-search" placeholder="бумага / ISIN…" value={q}
            onChange={(e) => setQ(e.target.value)} />
          <button className={"chip-btn" + (live ? " on" : "")} onClick={() => setLive((v) => !v)}
            title={`лента дотягивается сама раз в ${LIVE_MS / 1000} с; во вкладке в фоне опрос не идёт`}>
            {live ? "лайв" : "пауза"}
          </button>
          <button className="btn" onClick={() => setTick((t) => t + 1)}>Обновить</button>
        </div>
        <div className="ia-filters">
          {!daysView && (
            <>
              <span className="ia-flabel" title="R-spread сделки к индексу; строки без спреда фильтр отсекает">
                Спред, бп
              </span>
              <input className="tape-nin" type="number" placeholder="от" value={spreadMin}
                onChange={(e) => setSpreadMin(e.target.value)} />
              <input className="tape-nin" type="number" placeholder="до" value={spreadMax}
                onChange={(e) => setSpreadMax(e.target.value)} />
            </>
          )}
          <span className="ia-flabel" title="рейтинг по реестру; бумаги без рейтинга под фильтр не попадают">
            Рейтинг
          </span>
          {ratingOpts.map((r) => (
            <button key={r.name} className={"chip-btn" + (ratings.includes(r.name) ? " on" : "")}
              style={ratings.includes(r.name) ? undefined : { color: ratingColor(r.name) }}
              title={`${r.count} бумаг в справочниках`}
              onClick={() => setRatings((a) => toggle(a, r.name))}>{r.name}</button>
          ))}
          <span className="ia-flabel" title="срок до погашения по реестру; бумаги без даты погашения под фильтр не попадают">
            Срок, лет
          </span>
          <input className="tape-nin" type="number" step="0.5" min="0" placeholder="от"
            value={ttmMin} onChange={(e) => setTtmMin(e.target.value)} />
          <input className="tape-nin" type="number" step="0.5" min="0" placeholder="до"
            value={ttmMax} onChange={(e) => setTtmMax(e.target.value)} />
          {(spreadMin || spreadMax || ttmMin || ttmMax || ratings.length > 0) && (
            <button className="btn" onClick={() => {
              setSpreadMin(""); setSpreadMax(""); setTtmMin(""); setTtmMax("");
              setRatings([]);
            }}>сбросить</button>
          )}
          {sum.archive_till && (
            <span className="ia-flabel">данные до {sum.archive_till.slice(0, 16)}</span>
          )}
          {lastAt && (
            <span className="ia-flabel" title="время последнего успешного запроса">
              обновлено {lastAt.toLocaleTimeString("ru-RU")}
            </span>
          )}
        </div>
      </div>

      {status === "error" && <div className="ia-empty">ошибка: {errMsg}</div>}
      {status === "loading" && !data && !dayData && <div className="ia-empty">читаю архив сделок…</div>}

      {!daysView && data && (
        <>
          <div className="tape-sum">
            {isinReq && <span className="tape-kpi"><span className="tape-k">БУМАГА</span>
              <span className="tape-v">{rows[0]?.name || isinReq}</span></span>}
            <span className="tape-kpi"><span className="tape-k">СДЕЛОК</span><span className="tape-v">{fmt.num(sum.n, 0)}</span></span>
            <span className="tape-kpi"><span className="tape-k">ОБОРОТ, МЛН</span><span className="tape-v">{money(sum.value)}</span></span>
            <span className="tape-kpi"><span className="tape-k">BUY, МЛН</span><span className="tape-v tape-buy">{money(sum.buy_value)}</span></span>
            <span className="tape-kpi"><span className="tape-k">SELL, МЛН</span><span className="tape-v tape-sell">{money(sum.sell_value)}</span></span>
            <span className="tape-kpi"><span className="tape-k">АДРЕСНЫЕ, МЛН</span><span className="tape-v">{money(byM.ndm?.value)}</span></span>
            {sum.partial && <span className="ia-flabel">итоги по видимым строкам</span>}
            {(sum.top || []).length > 0 && !isinReq && (
              <span className="tape-top">
                <span className="tape-k">ТОП ОБОРОТА, МЛН</span>
                {(sum.top || []).slice(0, 5).map((t) => (
                  <button key={t.isin} className={"chip-btn" + (pin === t.isin ? " on" : "")}
                    title={`${t.emitter || t.isin} · ${t.n} сделок`}
                    onClick={() => setPin((p) => (p === t.isin ? null : t.isin))}>
                    {t.name} {money(t.value)}
                  </button>
                ))}
              </span>
            )}
            {pin && <button className="btn" onClick={() => setPin(null)}>снять бумагу</button>}
          </div>

          <div className="ia-table-wrap">
            <table className="grid tape-table">
              <thead>
                <tr>
                  <th className="left">ДАТА</th>
                  <th className="left">ВРЕМЯ</th>
                  <th className="left">БУМАГА</th>
                  <th className="left">РЕЖИМ</th>
                  <th>ЦЕНА, %</th>
                  <th>СУММА, МЛН</th>
                  <th className="left">СТОРОНА</th>
                  <th title="спред к индексу по ЦЕНЕ СДЕЛКИ (флоатеры от 1 млн ₽; у мелких принтов и фиксов — прочерк)">R-spread, бп</th>
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
                      title={`${r.isin} · ${r.ts} · ${r.board_title || r.board}`}>
                      <td className="left tape-ts">{dpart(r.ts)}</td>
                      <td className="left tape-ts">{tpart(r.ts)}</td>
                      <td className="left tape-name">
                        {r.name}
                        {r.rating && <span className="tape-rt" style={{ color: ratingColor(r.rating) }}>{r.rating}</span>}
                        {r.base && <span className="tape-base">{r.base === "FIXED" ? "фикс" : baseLabel(r.base)}</span>}
                      </td>
                      <td className="left blk-board">
                        <span className={r.negotiated ? "blk-tag blk-tag-ndm" : "blk-tag"}
                          title={r.board_title || r.board}>
                          {r.board_short || r.board}
                        </span>
                      </td>
                      <td className="num">{fmt.pct(r.price)}</td>
                      <td className="num tape-val">
                        {money(r.value)}{r.cur && r.cur !== "SUR" ? ` ${r.cur}` : ""}
                      </td>
                      <td className="left"><SideTag side={r.side} /></td>
                      <td className="num" style={r.y_idx_bps != null ? dmColor(r.y_idx_bps) : undefined}
                        title={r.dm_bps != null ? `DM ${fmt.num(r.dm_bps, 0)} бп` : undefined}>
                        {r.y_idx_bps != null ? fmt.num(r.y_idx_bps, 0) : "—"}
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
              показаны последние {fmt.num(rows.length, 0)} из {fmt.num(data.summary?.n, 0)}
              {LIMITS.filter((l) => l > limit).slice(0, 1).map((l) => (
                <button key={l} className="btn" onClick={() => setLimit(l)}>показать {fmt.num(l, 0)}</button>
              ))}
            </div>
          )}
        </>
      )}

      {daysView && dayData && (
        <>
          <div className="tape-sum">
            {isinReq && <span className="tape-kpi"><span className="tape-k">БУМАГА</span>
              <span className="tape-v">{dayRows[0]?.name || isinReq}</span></span>}
            <span className="tape-kpi"><span className="tape-k">БУМАГО-ДНЕЙ</span><span className="tape-v">{fmt.num(daySum.n, 0)}</span></span>
            <span className="tape-kpi"><span className="tape-k">СДЕЛОК</span><span className="tape-v">{fmt.num(daySum.trades, 0)}</span></span>
            <span className="tape-kpi"><span className="tape-k">ОБОРОТ, МЛН</span><span className="tape-v">{money(daySum.value)}</span></span>
          </div>
          <div className="ia-table-wrap">
            <table className="grid tape-table">
              <thead>
                <tr>
                  <th className="left">ДАТА</th>
                  <th className="left">БУМАГА</th>
                  <th className="left">РЕЖИМ</th>
                  <th>СДЕЛОК</th>
                  <th>СРЕДНЕВЗВЕС, %</th>
                  <th>ОБОРОТ, МЛН</th>
                </tr>
              </thead>
              <tbody>
                {dayRows.map((r) => (
                  <tr key={r.isin + r.date + r.board} title={`${r.isin} · ${r.board_title || r.board}`}>
                    <td className="left tape-ts">{fmt.date(r.date)}</td>
                    <td className="left tape-name">{r.name}</td>
                    <td className="left blk-board">
                      <span className="blk-tag blk-tag-ndm" title={r.board_title || r.board}>
                        {r.board_short || r.board}
                      </span>
                    </td>
                    <td className="num">{fmt.num(r.numtrades, 0)}</td>
                    <td className="num">{r.waprice != null ? fmt.pct(r.waprice) : "—"}</td>
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
