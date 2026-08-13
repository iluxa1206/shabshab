import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { fetchMarketTape, fetchBlockDays, fetchTapeIssuers, fetchTapeRatings } from "../api.js";
import { fmt, baseLabel, ratingColor, dmColor, yearsTo } from "../format.js";
import { copyText } from "../clipboard.js";
import { HeaderCell } from "./TableHeader.jsx";
import FiltersMenu from "./FiltersMenu.jsx";
import CouponFormula from "./CouponFormula.jsx";
import { usePageStatus } from "../pageStatus.jsx";

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

// Окна периода в фильтрах больше нет: лента всегда открыта на всю глубину
// архива, а вниз идёт страницами (курсор before_ts/before_id).
const MAX_DAYS = 400;
const PAGE = 500;
// РПС здесь = дневной агрегат адресных режимов (см. шапку файла)
const MARKETS = [[null, "все"], ["bonds", "Т+"], ["ndm", "РПС"]];
const SIDES = [[null, "любая"], ["buy", "buy"], ["sell", "sell"]];
// Охват. Дефолт — флоатеры: стол про них, а крупняк рынка в рублёвом объёме
// это почти целиком ОФЗ-ПД, и без фильтра лента вырождалась в ленту фиксов.
// «Весь рынок» остаётся как контекст (там же фиксы и бумаги вне реестра).
// Охват: два регистра бумаг. «Фиксы» — облигации с постоянным купоном
// (scope=fixed), а не «весь рынок»: рынок целиком в ленте всё равно виден
// поиском по конкретной бумаге.
const SCOPES = [["float", "флоатеры"], ["fixed", "фиксы"]];
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

/** Маркеры оферты — те же p/c, что в МОНИТОРЕ и по тем же правилам: p —
 *  ближайшая пут-оферта из MOEX bondization, c — колл эмитента (corpbonds;
 *  дата бывает не всегда — MOEX колл не различает). Факты независимые, у
 *  бумаги может быть и то и другое. Жирный маркер — метрики посчитаны к
 *  этому горизонту (правило цены vs цена выкупа). Место маркеров — у ДАТЫ
 *  ОФЕРТЫ; у погашения они остаются, только когда даты оферты нет. */
function OfferMarks({ r }) {
  const put = r.offer_kind === "put" && r.offer_date;
  const call = r.has_call === true || r.offer_kind === "call";
  if (!put && !call) return null;
  const hz = r.preferred_horizon;
  return (
    <>
      {put && <span className={"offer-mark offer-put" + (hz === "put" ? " offer-mark-on" : "")}
        title={"пут-оферта " + fmt.date(String(r.offer_date).slice(0, 10))}>p</span>}
      {call && <span className={"offer-mark offer-call" + (hz === "call" ? " offer-mark-on" : "")}
        title={r.offer_kind === "call" && r.offer_date
          ? "call-оферта " + fmt.date(String(r.offer_date).slice(0, 10)) : "call-опцион"}>c</span>}
    </>
  );
}

/** Погашение и оферта в одной ячейке, как в МОНИТОРЕ: дата + лет до неё в
 *  скобках, вторым этажом оферта мелким серым. СИНИЕ годы = горизонт, к
 *  которому посчитан спред строки — но только когда выбор был (без оферты и
 *  колла горизонт один, и подсветка каждой строки ничего не сообщает). */
function MaturityCell({ r }) {
  const mat = String(r.maturity).slice(0, 10);
  const off = r.offer_date ? String(r.offer_date).slice(0, 10) : null;
  const hz = r.preferred_horizon;
  const hasChoice = !!off || r.has_call === true;
  return (
    <div className="mat-cell">
      <div className="mat-main">
        {!off && <OfferMarks r={r} />}
        {fmt.date(mat)}
        {yearsTo(mat) != null && (
          <span className={"mat-yrs" + (hasChoice && hz === "maturity" ? " mat-hz" : "")}>
            {" (" + yearsTo(mat) + ")"}</span>
        )}
      </div>
      {off && (
        <div className="mat-offer"
          title={(r.offer_kind === "call" ? "call-оферта " : "пут-оферта ") + fmt.date(off)}>
          <OfferMarks r={r} />{fmt.date(off)}
          {yearsTo(off) != null && (
            <span className={"mat-yrs" + (hz === "put" || hz === "call" ? " mat-hz" : "")}>
              {" (" + yearsTo(off) + ")"}</span>
          )}
        </div>
      )}
    </div>
  );
}

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
  // формула купона — отдельной колонкой, как в СПИСКЕ: части выстраиваются
  // друг под другом («КС + 2,50% (12)»), а в имени выпуска ей тесно
  { key: "formula", label: "ФОРМУЛА",   align: "left", w: 15, get: (r) => r.margin_bps },
  { key: "isin",   label: "ISIN",       align: "left", w: 14, get: (r) => r.isin },
  // два этажа, как в МОНИТОРЕ: погашение с годами до него, под ним дата оферты
  // (если есть). Сортировка — по погашению: этаж оферты справочный. w=17 по
  // максимуму формата «p 10.10.2029 (4.2)».
  { key: "mat",    label: "ПОГАШЕНИЕ",  sub: "(ЛЕТ) · ОФЕРТА",
    align: "left", w: 17, get: (r) => r.maturity || "" },
  { key: "board",  label: "РЕЖИМ",      align: "left", w: 10, get: (r) => r.board_short || r.board || "" },
  { key: "price",  label: "ЦЕНА",  sub: "%",   align: "num",  w: 8,  get: (r) => r.price },
  { key: "value",  label: "СУММА", sub: "МЛН", align: "num",  w: 9,  get: (r) => r.value },
  { key: "side",   label: "СТОРОНА",           align: "left", w: 9,  get: (r) => r.side || "" },
  { key: "yidx",   label: "R-SPREAD", sub: "БП", align: "num", w: 10, get: (r) => r.y_idx_bps,
    title: "спред к индексу по ЦЕНЕ СДЕЛКИ (флоатеры от 1 млн ₽; у мелких принтов и фиксов — прочерк)" },
  { key: "yld",    label: "ДОХ-ТЬ", sub: "%",  align: "num",  w: 8,  get: (r) => r.yld },
  // график и карточка — последней колонкой: у имени они перетягивали взгляд,
  // а место в колонке БУМАГА нужнее самому имени
  { key: "act",    label: "",           align: "left", w: 15, get: () => null },
];
const DEFAULT_COLS = COLS.map((c) => c.key);
const LS_ORDER = "tapeCols";
// Фильтры ленты переживают уход на график/карточку и возврат «назад»: без
// этого стол пересобирал условия после каждого клика по бумаге.
const LS_FILTERS = "tapeFilters";

const readLS = (k, fallback) => {
  try { const v = JSON.parse(localStorage.getItem(k) || "null"); return v ?? fallback; }
  catch { return fallback; }
};
const savedFilters = () => readLS(LS_FILTERS, {}) || {};

// Набор по умолчанию — рабочий срез стола: крупные сделки во флоатерах без
// субордов. Он же встаёт по кнопке сброса. Заодно бережёт бэк: «весь рынок без
// порога» — это агрегат по миллиону с лишним сделок на каждый запрос.
const DEFAULTS = {
  minValue: 10e6, scopes: ["float"], hideSub: true, hideAmort: false,
  side: null, market: null, ratings: [], bases: [], cls: [], emitters: [],
  onlyWatch: false, spreadMin: "", spreadMax: "", ttmMin: "", ttmMax: "",
};
// поля объёма — в млн ₽; в состоянии и в API по-прежнему рубли
const mlnToRub = (v) => {
  const n = parseFloat(String(v).replace(",", "."));
  return Number.isFinite(n) && n > 0 ? Math.round(n * 1e6) : 0;
};
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

/** Кнопки строки: график во весь экран и стакан бумаги. Подписаны словами, а
 *  не значками — в плотной ленте иконку приходится расшифровывать наведением.
 *  Обе — обычная навигация, поэтому «назад» возвращает в ленту с её фильтрами. */
function RowLinks({ isin, onOpen }) {
  const stop = (e) => e.stopPropagation();
  return (
    <span className="tape-links">
      <button type="button" className="tape-link" title="График выпуска на весь экран"
        onClick={(e) => { stop(e); onOpen(isin, "chart"); }}>график</button>
      <button type="button" className="tape-link" title="Карточка бумаги со стаканом"
        onClick={(e) => { stop(e); onOpen(isin, "card"); }}>стакан</button>
    </span>
  );
}

/** Топ бумаг по обороту — выпадающим списком, а не строкой чипов: нужен
 *  изредка, а места занимал больше самой ленты. Выбор бумаги ПИШЕТ её ISIN В
 *  ПОИСК: одно поле — один способ сузить ленту, и сбрасывается фильтр там же,
 *  где виден. Своего состояния у списка нет — прежний внутренний «пин» мог
 *  спрятать саму кнопку, и снять фильтр было нечем. */
function TopTurnover({ top, current, onPick }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => { if (!box.current?.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);
  if (!top?.length) return null;
  const picked = current && top.find((t) => t.isin === current);
  return (
    <div className="fgroup tape-topbox" ref={box}>
      <button className={"chip-btn" + (open || picked ? " on" : "")}
        onClick={() => setOpen((v) => !v)}
        title="Бумаги с наибольшим оборотом в окне; выбор подставит ISIN в поиск">
        {picked ? `${picked.name} ▾` : "топ оборота ▾"}
      </button>
      {open && (
        <div className="tape-topmenu">
          {top.slice(0, 10).map((t) => (
            <button key={t.isin} className={"tape-topitem" + (current === t.isin ? " on" : "")}
              title={`${t.emitter || t.isin} · ${t.n} сделок`}
              onClick={() => { onPick(current === t.isin ? "" : t.isin); setOpen(false); }}>
              <span className="tt-name">{t.name}</span>
              <span className="tt-val">{money(t.value)}</span>
            </button>
          ))}
          {current && (
            <button className="tape-topitem tt-clear"
              onClick={() => { onPick(""); setOpen(false); }}>показать все бумаги</button>
          )}
        </div>
      )}
    </div>
  );
}

export default function TradesTape() {
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  // дефолт — рабочий срез: неделя и крупняк от 1 млн ₽. Максимум («макс» +
  // «все») лента тянет, но агрегат по миллиону сделок считается секундами.

  const [minValue, setMinValue] = useState(() => pick(savedFilters().minValue, DEFAULTS.minValue));
  const [side, setSide] = useState(() => pick(savedFilters().side, null));
  const [market, setMarket] = useState(() => pick(savedFilters().market, null));
  const [emitters, setEmitters] = useState(() => pick(savedFilters().emitters, []));
  // Охват — два независимых флажка: можно смотреть и флоатеры, и фиксы разом.
  // В API уезжает одним scope. Оба флажка (как и ни одного) → market: сужать
  // по списку ISIN тут нечем — вместе флоатеры и фиксы это и есть рынок, а
  // scope=universe гнал бы во временную таблицу весь реестр и вешал запрос.
  const [scopes, setScopes] = useState(() => pick(savedFilters().scopes, DEFAULTS.scopes));
  // страницы: rows копятся, курсор — последняя показанная сделка
  const [pages, setPages] = useState([]);       // догруженные порции
  const [more, setMore] = useState(false);      // есть ли ещё
  const [loadingMore, setLoadingMore] = useState(false);
  const [spreadMin, setSpreadMin] = useState(() => pick(savedFilters().spreadMin, ""));
  const [spreadMax, setSpreadMax] = useState(() => pick(savedFilters().spreadMax, ""));
  const [ttmMin, setTtmMin] = useState(() => pick(savedFilters().ttmMin, ""));
  const [ttmMax, setTtmMax] = useState(() => pick(savedFilters().ttmMax, ""));
  const [ratings, setRatings] = useState(() => pick(savedFilters().ratings, []));   // выбранные грейды
  // признаки выпуска — те же, что в СПИСКЕ (внутри воронки)
  const [bases, setBases] = useState(() => pick(savedFilters().bases, []));
  const [cls, setCls] = useState(() => pick(savedFilters().cls, []));
  const [hideSub, setHideSub] = useState(() => pick(savedFilters().hideSub, DEFAULTS.hideSub));
  const [hideAmort, setHideAmort] = useState(() => pick(savedFilters().hideAmort, false));
  // Избранное общее со СПИСКОМ: watchlist живёт в localStorage под ключом
  // "watch", своей копии у ленты нет — звезда там и здесь означает одно и то же.
  const [onlyWatch, setOnlyWatch] = useState(() => pick(savedFilters().onlyWatch, false));
  const watch = useMemo(() => readLS("watch", []) || [], []);
  const [ratingOpts, setRatingOpts] = useState([]);
  // «по дням» — агрегат бумага/режим/день вместо поштучной ленты. Только для
  // адресных: у безадресных поштучный архив полный, агрегировать нечего.
  const [byDay, setByDay] = useState(() => pick(savedFilters().byDay, false));
  // ?q= — вход с бумагой из карточки (кнопка СДЕЛКИ): лента открывается сразу
  // сужённой на этот выпуск. Дальше поле живёт своей жизнью, адрес не трогаем.
  const [q, setQ] = useState(() => sp.get("q") || "");
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
  const isinReq = qIsin;
  const scope = scopes.length === 1 ? scopes[0] : "market";
  const daysView = market === "ndm" && byDay;

  useEffect(() => {
    localStorage.setItem(LS_FILTERS, JSON.stringify({
      minValue, side, market, emitters, scopes, spreadMin, spreadMax,
      ttmMin, ttmMax, ratings, byDay, bases, cls, hideSub, hideAmort,
      onlyWatch }));
  }, [minValue, side, market, emitters, scopes, spreadMin, spreadMax,
      ttmMin, ttmMax, ratings, byDay, bases, cls, hideSub, hideAmort,
      onlyWatch]);
  useEffect(() => { localStorage.setItem(LS_ORDER, JSON.stringify(colOrder)); }, [colOrder]);

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
      ? fetchBlockDays({ isin: isinReq, days: MAX_DAYS, minValue: minValue || 1e6,
                         scope, issuer: emitters, ttmMin: num(ttmMin),
                         ttmMax: num(ttmMax), rating: ratings, limit: PAGE }, ac.signal)
        .then((d) => { setDayData(d); })
      : fetchMarketTape({ days: MAX_DAYS, minValue, side, market, issuer: emitters,
                          isin: isinReq, scope, limit: PAGE, spreadMin: num(spreadMin),
                          spreadMax: num(spreadMax), ttmMin: num(ttmMin),
                          ttmMax: num(ttmMax), rating: ratings, base: bases, cls,
                          hideSubord: hideSub, hideAmort,
                          isins: onlyWatch ? watch : undefined }, ac.signal)
        .then((d) => { setData(d); setPages([]); setMore(!!d.has_more); });
    req.then(() => { setStatus("ready"); setLastAt(new Date()); })
      .catch((e) => { if (e.name !== "AbortError") { setErrMsg(e.message); setStatus("error"); } });
    return () => ac.abort();
  }, [minValue, side, market, daysView, emitters, isinReq, scopes, onlyWatch,
      spreadMin, spreadMax, ttmMin, ttmMax, ratings, bases, cls, hideSub, hideAmort, tick]);

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
  const allRows = useMemo(
    () => [...(data?.trades || []), ...pages.flat()], [data, pages]);
  const rows = useMemo(() => allRows.filter(match), [allRows, q, qIsin]);
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

  // Топ показываем ПОСЛЕДНИЙ ПОЛНЫЙ: как только лента сужена до одной бумаги,
  // бэк присылает топ из неё же — список схлопывался в одну строку, и выбрать
  // другую бумагу было нельзя.
  const [topList, setTopList] = useState([]);
  useEffect(() => {
    const t = data?.summary?.top;
    if (!isinReq && t?.length) setTopList(t);
  }, [data, isinReq]);

  const daySum = useMemo(() => {
    let n = 0, value = 0, trades = 0;
    for (const r of dayRows) { n += 1; value += r.value || 0; trades += r.numtrades || 0; }
    return { n, value, trades };
  }, [dayRows]);

  // Итоги окна уходят в ОБЩУЮ нижнюю полосу приложения (там же тема, даты,
  // часы) — своей строки у вкладки нет: две полосы подряд съедали высоту и
  // выглядели как случайно вклиненная плашка.
  usePageStatus(daysView
    ? [
      isinReq && { k: "БУМАГА", v: dayRows[0]?.name || isinReq },
      { k: "БУМАГО-ДНЕЙ", v: fmt.num(daySum.n, 0) },
      { k: "СДЕЛОК", v: fmt.num(daySum.trades, 0) },
      { k: "ОБОРОТ", v: money(daySum.value), title: "млн ₽" },
    ]
    : [
      isinReq && { k: "БУМАГА", v: rows[0]?.name || isinReq },
      { k: "СДЕЛОК", v: fmt.num(sum.n, 0),
        title: sum.partial ? "итоги по видимым строкам (текстовый поиск)" : "все сделки окна" },
      { k: "ОБОРОТ", v: money(sum.value), title: "млн ₽, только рублёвые выпуски" },
      { k: "BUY", v: money(sum.buy_value), cls: "tape-buy", title: "млн ₽" },
      { k: "SELL", v: money(sum.sell_value), cls: "tape-sell", title: "млн ₽" },
      { k: "РПС", v: money(byM.ndm?.value), title: "оборот адресных режимов, млн ₽" },
      { k: "ПОКАЗАНО", v: data?.summary?.n
        ? `${fmt.num(allRows.length, 0)}/${fmt.num(data.summary.n, 0)}`
        : fmt.num(allRows.length, 0),
        title: "строк в таблице / всего под фильтром" },
      // opt — второстепенное: на узкой полосе прячется первым, чтобы часы и
      // даты приложения не уезжали за край
      sum.archive_till && { k: "ДО", v: sum.archive_till.slice(5, 16), opt: true,
        title: "последняя сделка в архиве" },
      lastAt && { k: "ОБНОВЛ.", v: lastAt.toLocaleTimeString("ru-RU"), opt: true,
        title: "время последнего успешного запроса" },
    ]);

  const toggle = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  // Следующая страница: курсор — последняя УЖЕ показанная сделка. Страницы
  // копятся в pages, потому что лента живая: перезапрос с бОльшим лимитом
  // сдвинул бы всё, что успело прийти сверху.
  const loadMore = async () => {
    const last = allRows[allRows.length - 1];
    if (!last || loadingMore) return;
    setLoadingMore(true);
    try {
      const d = await fetchMarketTape({
        days: MAX_DAYS, minValue, side, market, issuer: emitters, isin: isinReq,
        scope, limit: PAGE, spreadMin: num(spreadMin), spreadMax: num(spreadMax),
        ttmMin: num(ttmMin), ttmMax: num(ttmMax), rating: ratings, base: bases, cls,
        hideSubord: hideSub, hideAmort,
        isins: onlyWatch ? watch : undefined,
        beforeTs: last.ts, beforeId: last.trade_id });
      setPages((ps) => [...ps, d.trades || []]);
      setMore(!!d.has_more);
    } catch (e) {
      setErrMsg(e.message);
    } finally {
      setLoadingMore(false);
    }
  };

  // Счётчик на воронке — сколько условий включено ВСЕГО (как в СПИСКЕ), в пару
  // к кнопке сброса: иначе спрятанный в меню фильтр молча режет ленту.
  // Счётчик и кнопка сброса показывают ОТКЛОНЕНИЯ от набора по умолчанию:
  // «10 млн + флоатеры + без субордов» — это норма стола, а не три фильтра.
  const activeFilters = bases.length + cls.length + emitters.length + ratings.length
    + (hideSub === DEFAULTS.hideSub ? 0 : 1) + (hideAmort === DEFAULTS.hideAmort ? 0 : 1)
    + (onlyWatch ? 1 : 0) + (side ? 1 : 0) + (market ? 1 : 0)
    + (minValue === DEFAULTS.minValue ? 0 : 1)
    + (spreadMin ? 1 : 0) + (spreadMax ? 1 : 0) + (ttmMin ? 1 : 0) + (ttmMax ? 1 : 0)
    + (scopes.length === 1 && scopes[0] === DEFAULTS.scopes[0] ? 0 : 1);

  // Сброс возвращает НАБОР ПО УМОЛЧАНИЮ, а не пустоту: пустой фильтр — это
  // весь рынок без порога, самый тяжёлый и наименее рабочий срез.
  const resetFilters = () => {
    setSpreadMin(DEFAULTS.spreadMin); setSpreadMax(DEFAULTS.spreadMax);
    setTtmMin(DEFAULTS.ttmMin); setTtmMax(DEFAULTS.ttmMax);
    setRatings(DEFAULTS.ratings); setBases(DEFAULTS.bases); setCls(DEFAULTS.cls);
    setEmitters(DEFAULTS.emitters); setOnlyWatch(DEFAULTS.onlyWatch);
    setHideSub(DEFAULTS.hideSub); setHideAmort(DEFAULTS.hideAmort);
    setSide(DEFAULTS.side); setMarket(DEFAULTS.market);
    setMinValue(DEFAULTS.minValue);
    setScopes(DEFAULTS.scopes);
  };

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

  // Открыть бумагу из ленты. Карточка — ПОВЕРХ ленты: Drawer смонтирован
  // глобально и слушает ?isin= в адресе, поэтому уходить на СПИСОК незачем —
  // лента остаётся под карточкой, а её закрытие ничего не пересобирает.
  // График — отдельная страница, но возврат с неё идёт назад по истории.
  const openBond = (isin, where) => {
    if (where === "chart") { nav(`/chart/${isin}`); return; }
    setSp((prev) => {
      const n = new URLSearchParams(prev);
      n.set("isin", isin);
      n.set("ob", "1");
      return n;
    });
  };

  return (
    <div className="issuer-agg tape-page">
      <div className="ia-head">
        <h2 className="ia-title">Лента сделок</h2>
        <div className="ia-filters">
          {/* Группы и порядок — как в тулбаре МОНИТОРА: поиск, избранное,
              воронка со сбросом, рейтинги, объём, срок, спред. */}
          <input className="tape-search" placeholder="бумага / ISIN…" value={q}
            onChange={(e) => setQ(e.target.value)} />

          <div className="fgroup">
            <button className={"chip-btn" + (onlyWatch ? " on" : "")}
              onClick={() => setOnlyWatch((v) => !v)}
              title="Только избранные бумаги — тот же watchlist, что в МОНИТОРЕ">
              ★ {watch.length}
            </button>
          </div>

          <div className="fgroup">
            {/* воронка: база, тип выпуска, класс, режим торгов, сторона, эмитент */}
            <FiltersMenu
              basesSel={bases} toggleBase={(b) => setBases((a) => toggle(a, b))}
              clearBases={() => setBases([])}
              issuers={issuers} emittersSel={emitters}
              toggleEmitter={(n) => setEmitters((a) => toggle(a, n))}
              clearEmitters={() => setEmitters([])}
              hideSub={hideSub} setHideSub={setHideSub}
              hideAmort={hideAmort} setHideAmort={setHideAmort}
              clsSel={cls} toggleCls={(c) => setCls((a) => toggle(a, c))}
              activeCount={activeFilters}
              extra={(
                <>
                  <div className="fp-sec">
                    <div className="fp-head"><span className="fg-lbl">РЕЖИМ ТОРГОВ</span></div>
                    <div className="fp-chips">
                      {MARKETS.map(([v, label]) => (
                        <button key={label} className={"chip-btn" + (market === v ? " on" : "")}
                          onClick={() => setMarket(v)}
                          title={v === "ndm" ? "адресные режимы: РПС, РПС с ЦК, размещения, выкупы"
                            : v === "bonds" ? "безадресный стакан" : "все режимы"}>{label}</button>
                      ))}
                      {market === "ndm" && (
                        <button className={"chip-btn" + (byDay ? " on" : "")}
                          onClick={() => setByDay((v) => !v)}
                          title="агрегат бумага/режим/день из ISS — единственный след адресных сделок за прошлые сессии">
                          по дням
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="fp-sec">
                    <div className="fp-head"><span className="fg-lbl">ОХВАТ</span></div>
                    <div className="fp-chips">
                      {SCOPES.map(([v, label]) => (
                        <button key={v} className={"chip-btn" + (scopes.includes(v) ? " on" : "")}
                          aria-pressed={scopes.includes(v)}
                          onClick={() => setScopes((a) => toggle(a, v))}
                          title={v === "float" ? "флоатеры (KEYRATE/RUONIA) из реестра"
                            : "фиксы: облигации с постоянным купоном"}>{label}</button>
                      ))}
                    </div>
                    <div className="sig-note">
                      Оба флажка (или ни одного) — рынок целиком, включая бумаги вне реестра.
                    </div>
                  </div>

                  <div className="fp-sec">
                    <div className="fp-head"><span className="fg-lbl">ОБНОВЛЕНИЕ</span></div>
                    <div className="fp-chips">
                      <button className={"chip-btn" + (live ? " on" : "")}
                        onClick={() => setLive((v) => !v)}
                        title={`лента дотягивается сама раз в ${LIVE_MS / 1000} с; во вкладке в фоне опрос не идёт`}>
                        {live ? "лайв включён" : "лайв выключен"}
                      </button>
                      <button className="chip-btn" onClick={() => setTick((t) => t + 1)}>
                        обновить сейчас
                      </button>
                    </div>
                  </div>

                  <div className="fp-sec">
                    <div className="fp-head"><span className="fg-lbl">СТОРОНА</span></div>
                    <div className="fp-chips">
                      {SIDES.map(([v, label]) => (
                        <button key={label} className={"chip-btn" + (side === v ? " on" : "")}
                          onClick={() => setSide(v)}
                          title="агрессор — сторона, забравшая заявку; у адресных сделок его нет">
                          {label}</button>
                      ))}
                    </div>
                  </div>
                </>
              )} />
            <button className="chip-btn reset-btn" disabled={!activeFilters}
              onClick={resetFilters} aria-label="Сбросить все фильтры"
              title="Снять все фильтры ленты">✕</button>
          </div>

          <div className="fgroup">
            {ratingOpts.map((r) => (
              <button key={r.name} className={"chip-btn" + (ratings.includes(r.name) ? " on" : "")}
                style={ratings.includes(r.name) ? undefined : { color: ratingColor(r.name) }}
                title={`${r.count} бумаг в справочниках`}
                onClick={() => setRatings((a) => toggle(a, r.name))}>{r.name}</button>
            ))}
          </div>

          <div className="fgroup" title="Нижний порог суммы сделки, млн ₽. Верхней границы нет: крупные принты — то, ради чего лента и открыта.">
            <span className="fg-lbl">ОБЪЁМ ОТ, МЛН</span>
            <input className="num-input" type="number" min="0" step="0.5" placeholder="все"
              aria-label="Объём сделки от, млн ₽"
              value={minValue ? String(minValue / 1e6) : ""}
              onChange={(e) => setMinValue(mlnToRub(e.target.value))} />
            {minValue !== DEFAULTS.minValue && (
              <button className="chip-btn" title="Вернуть порог по умолчанию"
                onClick={() => setMinValue(DEFAULTS.minValue)}>×</button>
            )}
          </div>

          <div className="fgroup" title="Лет до даты, к которой посчитаны метрики бумаги (оферта/колл по правилу цены, иначе погашение), в интервале [от, до]; бумаги без даты при заданной границе скрыты">
            <span className="fg-lbl">СРОК, Y</span>
            <input className="num-input" type="number" min="0" step="0.5" placeholder="от"
              value={ttmMin} onChange={(e) => setTtmMin(e.target.value)} />
            <span className="fg-lbl">—</span>
            <input className="num-input" type="number" min="0" step="0.5" placeholder="до"
              value={ttmMax} onChange={(e) => setTtmMax(e.target.value)} />
            {(ttmMin || ttmMax) && (
              <button className="chip-btn" title="Сбросить окно срока"
                onClick={() => { setTtmMin(""); setTtmMax(""); }}>×</button>
            )}
          </div>

          <div className="fgroup" title="R-spread сделки в интервале [от, до], бп; сделки без посчитанного спреда при заданной границе скрыты">
            <span className="fg-lbl">R-SPREAD</span>
            <input className="num-input" type="number" step="10" placeholder="от"
              value={spreadMin} onChange={(e) => setSpreadMin(e.target.value)} />
            <span className="fg-lbl">—</span>
            <input className="num-input" type="number" step="10" placeholder="до"
              value={spreadMax} onChange={(e) => setSpreadMax(e.target.value)} />
            {(spreadMin || spreadMax) && (
              <button className="chip-btn" title="Сбросить окно спреда"
                onClick={() => { setSpreadMin(""); setSpreadMax(""); }}>×</button>
            )}
          </div>

          {!daysView && <TopTurnover top={topList} current={qIsin} onPick={setQ} />}
        </div>
      </div>

      {status === "error" && <div className="ia-empty">ошибка: {errMsg}</div>}
      {status === "loading" && !data && !dayData && <div className="ia-empty">читаю архив сделок…</div>}

      {!daysView && data && (
        <>
          <div className="ia-table-wrap">
            <table className="grid tape-table packed cols-fixed">
              <colgroup>
                {cols.map((c) => <col key={c.key} style={{ "--cw": (c.w || 8) + "ch" }} />)}
                <col className="col-fill" />
              </colgroup>
              <thead>
                <tr>
                  {cols.map((c) => <HeaderCell key={c.key} col={c} sort={sort} onSort={onSort}
                    onMoveCol={onMoveCol} dragRef={dragRef} dragKey={dragKey} setDragKey={setDragKey}
                    overKey={overKey} setOverKey={setOverKey} />)}
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
                        {r.name}
                        {r.rating && <span className="tape-rt" style={{ color: ratingColor(r.rating) }}>{r.rating}</span>}
                      </span>
                    ),
                    formula: r.base === "FIXED"
                      ? <span className="tape-base">фикс</span>
                      : <CouponFormula base={r.base} spreadBps={r.margin_bps}
                          couponsPerYear={r.coupons_per_year} formula={r.coupon_text} />,
                    isin: <IsinCell isin={r.isin} />,
                    mat: r.maturity ? <MaturityCell r={r} /> : <span className="dash">—</span>,
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
                    act: <RowLinks isin={r.isin} onOpen={openBond} />,
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

          {more && (
            <div className="tape-more">
              <button className="btn" disabled={loadingMore} onClick={loadMore}>
                {loadingMore ? "грузим…" : `показать ещё ${fmt.num(PAGE, 0)}`}
              </button>
            </div>
          )}
        </>
      )}

      {daysView && dayData && (
        <>
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
