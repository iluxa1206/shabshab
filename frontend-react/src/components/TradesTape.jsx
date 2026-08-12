import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchMarketTape, fetchBlockDays, fetchTapeIssuers, fetchTapeRatings } from "../api.js";
import { fmt, baseLabel, ratingColor, dmColor } from "../format.js";
import { copyText } from "../clipboard.js";
import { HeaderCell } from "./TableHeader.jsx";
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

// ── колонки ленты ───────────────────────────────────────────────────────────
// Порядок, ширины и сортировка — как в СПИСКЕ (общий HeaderCell): переносятся
// перетаскиванием, тянутся за границу, кликом сортируются. Раскладка живёт в
// localStorage, чтобы стол не собирал её заново каждое утро.
const COLS = [
  { key: "date",   label: "ДАТА",       align: "left", w: 6,  get: (r) => r.ts },
  { key: "time",   label: "ВРЕМЯ",      align: "left", w: 8,  get: (r) => r.ts },
  { key: "name",   label: "БУМАГА",     align: "left", w: 20, get: (r) => (r.name || "").toLowerCase() },
  { key: "isin",   label: "ISIN",       align: "left", w: 14, get: (r) => r.isin },
  { key: "mat",    label: "ПОГАШЕНИЕ",  align: "left", w: 11, get: (r) => r.maturity || "" },
  { key: "board",  label: "РЕЖИМ",      align: "left", w: 9,  get: (r) => r.board_short || r.board || "" },
  { key: "price",  label: "ЦЕНА, %",    align: "num",  w: 8,  get: (r) => r.price },
  { key: "value",  label: "СУММА, МЛН", align: "num",  w: 11, get: (r) => r.value },
  { key: "side",   label: "СТОРОНА",    align: "left", w: 8,  get: (r) => r.side || "" },
  { key: "yidx",   label: "R-SPREAD, БП", align: "num", w: 12, get: (r) => r.y_idx_bps,
    title: "спред к индексу по ЦЕНЕ СДЕЛКИ (флоатеры от 1 млн ₽; у мелких принтов и фиксов — прочерк)" },
  { key: "yld",    label: "ДОХ-ТЬ, %",  align: "num",  w: 8,  get: (r) => r.yld },
];
const DEFAULT_COLS = COLS.map((c) => c.key);
const LS_ORDER = "tapeCols";
const LS_WIDTHS = "tapeColW";
// Фильтры ленты переживают уход на график/карточку и возврат «назад»: без
// этого стол пересобирал условия после каждого клика по бумаге.
const LS_FILTERS = "tapeFilters";

const readLS = (k, fallback) => {
  try { const v = JSON.parse(localStorage.getItem(k) || "null"); return v ?? fallback; }
  catch { return fallback; }
};
const savedFilters = () => readLS(LS_FILTERS, {}) || {};
const pick = (v, fallback) => (v === undefined || v === null ? fallback : v);

/** ISIN с копированием по клику: в ленте он нужен, чтобы утащить бумагу в
 *  чужую систему, а выделять мышью в плотной таблице неудобно. */
function IsinCell({ isin }) {
  const [state, setState] = useState("");
  if (!isin) return <span className="dash">—</span>;
  const onClick = async (e) => {
    e.stopPropagation();
    const ok = await copyText(isin);
    setState(ok ? "ok" : "err");
    setTimeout(() => setState(""), 1200);
  };
  return (
    <button type="button" className={"tape-isin" + (state ? " " + state : "")}
      onClick={onClick} title="Клик — скопировать ISIN">
      {state === "ok" ? "скопирован" : state === "err" ? "не вышло" : isin}
    </button>
  );
}

/** Кнопки у названия: график во весь экран и карточка со стаканом. Обе —
 *  обычная навигация, поэтому «назад» возвращает в ленту, а фильтры она
 *  держит в URL и восстанавливает сама. */
function RowLinks({ isin, onOpen }) {
  const stop = (e) => e.stopPropagation();
  return (
    <span className="tape-links">
      <button type="button" className="tape-link" title="График выпуска на весь экран"
        onClick={(e) => { stop(e); onOpen(isin, "chart"); }}>◱</button>
      <button type="button" className="tape-link" title="Карточка бумаги со стаканом"
        onClick={(e) => { stop(e); onOpen(isin, "card"); }}>▤</button>
    </span>
  );
}

export default function TradesTape() {
  const nav = useNavigate();
  // дефолт — рабочий срез: неделя и крупняк от 1 млн ₽. Максимум («макс» +
  // «все») лента тянет, но агрегат по миллиону сделок считается секундами.
  const [days, setDays] = useState(() => pick(savedFilters().days, 7));
  const [minValue, setMinValue] = useState(() => pick(savedFilters().minValue, 1e6));
  const [side, setSide] = useState(() => pick(savedFilters().side, null));
  const [market, setMarket] = useState(() => pick(savedFilters().market, null));
  const [emitters, setEmitters] = useState(() => pick(savedFilters().emitters, []));
  const [scope, setScope] = useState(() => pick(savedFilters().scope, "float"));
  const [limit, setLimit] = useState(() => pick(savedFilters().limit, LIMITS[0]));
  const [spreadMin, setSpreadMin] = useState(() => pick(savedFilters().spreadMin, ""));
  const [spreadMax, setSpreadMax] = useState(() => pick(savedFilters().spreadMax, ""));
  const [ttmMin, setTtmMin] = useState(() => pick(savedFilters().ttmMin, ""));
  const [ttmMax, setTtmMax] = useState(() => pick(savedFilters().ttmMax, ""));
  const [ratings, setRatings] = useState(() => pick(savedFilters().ratings, []));   // выбранные грейды
  const [ratingOpts, setRatingOpts] = useState([]);
  const [pin, setPin] = useState(() => pick(savedFilters().pin, null));
  // «по дням» — агрегат бумага/режим/день вместо поштучной ленты. Только для
  // адресных: у безадресных поштучный архив полный, агрегировать нечего.
  const [byDay, setByDay] = useState(() => pick(savedFilters().byDay, false));
  const [q, setQ] = useState("");
  const [data, setData] = useState(null);
  const [dayData, setDayData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [errMsg, setErrMsg] = useState("");
  const [issuers, setIssuers] = useState([]);
  const [tick, setTick] = useState(0);
  const [live, setLive] = useState(true);
  const [lastAt, setLastAt] = useState(null);
  // раскладка колонок: порядок и ширины переживают перезагрузку
  const [colOrder, setColOrder] = useState(() => {
    const saved = readLS(LS_ORDER, null);
    const known = new Set(DEFAULT_COLS);
    const kept = Array.isArray(saved) ? saved.filter((k) => known.has(k)) : [];
    // новые колонки версии дописываем в конец, а не теряем молча
    return kept.length ? [...kept, ...DEFAULT_COLS.filter((k) => !kept.includes(k))] : DEFAULT_COLS;
  });
  const [colWidths, setColWidths] = useState(() => readLS(LS_WIDTHS, {}));
  const [sort, setSort] = useState({ key: null, dir: "desc" });
  const dragRef = useRef(null);
  const [dragKey, setDragKey] = useState(null);
  const [overKey, setOverKey] = useState(null);
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
    localStorage.setItem(LS_FILTERS, JSON.stringify({
      days, minValue, side, market, emitters, scope, limit, spreadMin, spreadMax,
      ttmMin, ttmMax, ratings, byDay, pin }));
  }, [days, minValue, side, market, emitters, scope, limit, spreadMin, spreadMax,
      ttmMin, ttmMax, ratings, byDay, pin]);
  useEffect(() => { localStorage.setItem(LS_ORDER, JSON.stringify(colOrder)); }, [colOrder]);
  useEffect(() => { localStorage.setItem(LS_WIDTHS, JSON.stringify(colWidths)); }, [colWidths]);

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

  // ── колонки: порядок, ширина, сортировка ─────────────────────────────────
  const cols = useMemo(() => {
    const byKey = new Map(COLS.map((c) => [c.key, c]));
    const out = colOrder.map((k) => byKey.get(k)).filter(Boolean);
    return out.length ? out : COLS;
  }, [colOrder]);

  const onSort = (key) => setSort((s0) => (s0.key === key
    ? { key, dir: s0.dir === "desc" ? "asc" : "desc" }
    : { key, dir: "desc" }));

  const onMoveCol = (from, to) => setColOrder((order) => {
    const next = [...order];
    const i = next.indexOf(from);
    if (i < 0) return order;
    next.splice(i, 1);
    if (to === "-1" || to === "+1") {
      next.splice(Math.max(0, Math.min(next.length, i + (to === "-1" ? -1 : 1))), 0, from);
    } else {
      const j = next.indexOf(to);
      next.splice(j < 0 ? next.length : j, 0, from);
    }
    return next;
  });
  const onResizeCol = (key, px) => setColWidths((w) => ({ ...w, [key]: px }));
  const onResetColWidth = (key) => setColWidths((w) => {
    const next = { ...w }; delete next[key]; return next;
  });

  // Сортировка КЛИЕНТСКАЯ, по загруженным строкам: сервер отдаёт срез по
  // времени, и пересортировка всего окна на бэке ради колонки не нужна.
  const sorted = useMemo(() => {
    if (!sort.key) return rows;
    const col = COLS.find((c) => c.key === sort.key);
    if (!col) return rows;
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const x = col.get(a), y = col.get(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;            // пустые всегда внизу
      if (y == null) return -1;
      if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
      return String(x).localeCompare(String(y), "ru") * sign;
    });
  }, [rows, sort]);

  // Открыть бумагу из ленты. Фильтры лежат в URL, поэтому «назад» из графика
  // или карточки возвращает ленту в том же виде — без пересбора условий.
  const openBond = (isin, where) => {
    if (where === "chart") nav(`/chart/${isin}`);
    else nav(`/floaters?isin=${isin}&ob=1`);
  };

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
            <table className="grid tape-table cols-fixed">
              <colgroup>
                {cols.map((c) => <col key={c.key} style={colWidths[c.key]
                  ? { "--cw": colWidths[c.key] + "px" }
                  : { "--cw": (c.w || 8) + "ch" }} />)}
                <col className="fill-col" />
              </colgroup>
              <thead>
                <tr>
                  {cols.map((c) => <HeaderCell key={c.key} col={c} sort={sort} onSort={onSort}
                    onMoveCol={onMoveCol} dragRef={dragRef} dragKey={dragKey} setDragKey={setDragKey}
                    overKey={overKey} setOverKey={setOverKey}
                    onResizeCol={onResizeCol} onResetColWidth={onResetColWidth} />)}
                  <th className="fill-col" />
                </tr>
              </thead>
              <tbody>
                {sorted.map((r) => {
                  // график выпуска построен вокруг флоатера — для фиксов/ОФЗ и
                  // бумаг вне юниверса он пустой, такие строки никуда не ведут
                  const clickable = r.base === "KEYRATE" || r.base === "RUONIA";
                  const cell = {
                    date: <span className="tape-ts">{dpart(r.ts)}</span>,
                    time: <span className="tape-ts">{tpart(r.ts)}</span>,
                    name: (
                      <span className="tape-name-cell">
                        <RowLinks isin={r.isin} onOpen={openBond} />
                        {r.name}
                        {r.rating && <span className="tape-rt" style={{ color: ratingColor(r.rating) }}>{r.rating}</span>}
                        {r.base && <span className="tape-base">{r.base === "FIXED" ? "фикс" : baseLabel(r.base)}</span>}
                      </span>
                    ),
                    isin: <IsinCell isin={r.isin} />,
                    mat: r.maturity ? fmt.date(String(r.maturity).slice(0, 10)) : <span className="dash">—</span>,
                    board: (
                      <span className={r.negotiated ? "blk-tag blk-tag-ndm" : "blk-tag"}
                        title={r.board_title || r.board}>
                        {r.board_short || r.board}
                      </span>
                    ),
                    price: fmt.pct(r.price),
                    value: <>{money(r.value)}{r.cur && r.cur !== "SUR" ? ` ${r.cur}` : ""}</>,
                    side: <SideTag side={r.side} />,
                    yidx: r.y_idx_bps != null ? fmt.num(r.y_idx_bps, 0) : "—",
                    yld: r.yld != null ? fmt.num(r.yld, 2) : "—",
                  };
                  return (
                    <tr key={r.trade_id}
                      className={(clickable ? "" : "tape-row-static ")
                                 + (r.negotiated ? "blk-ndm" : "")}
                      onClick={clickable ? () => openBond(r.isin, "chart") : undefined}
                      title={`${r.isin} · ${r.ts} · ${r.board_title || r.board}`}>
                      {cols.map((c) => (
                        <td key={c.key}
                          className={c.align === "left" ? "left" : c.align === "num" ? "num" : ""}
                          style={c.key === "yidx" && r.y_idx_bps != null ? dmColor(r.y_idx_bps) : undefined}
                          title={c.key === "yidx" && r.dm_bps != null
                            ? `DM ${fmt.num(r.dm_bps, 0)} бп` : undefined}>
                          {cell[c.key]}
                        </td>
                      ))}
                      <td className="fill-col" />
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
