import { cloneElement, memo, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { baseLabel, fmt, dmColor, ratingColor, yearsTo } from "../format.js";
import { fetchAlerts } from "../api.js";
import { copyText } from "../clipboard.js";
import CouponFormula from "./CouponFormula.jsx";
import { HeaderCell } from "./TableHeader.jsx";

const D = () => <span className="dash">—</span>;

// ISIN под именем выпуска: клик копирует его в буфер. stopPropagation — иначе
// клик уходит в строку и открывает карточку вместо копирования.
function IsinCopy({ isin }) {
  const [state, setState] = useState(""); // "" | ok | err
  if (!isin) return null;
  const onClick = async (e) => {
    e.stopPropagation();
    const ok = await copyText(isin);
    setState(ok ? "ok" : "err");
    setTimeout(() => setState(""), 1200);
  };
  return (
    <button type="button" className={"isin-copy" + (state ? " " + state : "")} onClick={onClick}
      title={state === "err" ? "Не удалось скопировать" : `${isin} — скопировать`}>
      {state === "ok" ? "скопировано" : state === "err" ? "не вышло" : isin}
    </button>
  );
}

// WS тикнул цену, но производные метрики (DM/SM/z/dirty/CHG/Y-IDX) ещё
// от прошлого расчёта бэка → dim-класс, чтобы трейдер не читал их как актуальные.
const ms = (b) => (b._mstale ? " mstale" : "");

function Chip({ value }) {
  if (value == null) return <D />;
  return <span className="dm-chip" style={dmColor(value)}>{fmt.bps(value)} {value >= 0 ? "▲" : "▼"}</span>;
}

// Отклонение текущего R-spread от базы прошлой недели, bps. Двумя этажами:
// сверху отклонение со знаком (крашено как спред — шире базы = дороже риск),
// снизу сама база. Одна цифра без базы обманчива: +40 к базе 120 и +40 к базе
// 900 — разные истории, поэтому база рисуется рядом, а не прячется в title.
export function dev7(b) {
  const cur = b.yield_over_index_bps, base = b.y_idx_avg7_bps;
  return cur == null || base == null ? null : cur - base;
}

function Dev7({ b }) {
  const base = b.y_idx_avg7_bps, d = dev7(b);
  const title = "отклонение текущего R-spread от средневзвешенного по обороту"
    + " спреда за предыдущие 7 дней (по средневзвешенной цене часа, без сегодня)";
  if (d == null && base == null) return <td className={"num" + ms(b)} title={title}><D /></td>;
  return (
    <td className={"num q-cell" + ms(b)} title={title}>
      <div className="q-px q-dev" style={d == null ? undefined : dmColor(Math.round(d) || null)}>
        {d == null ? <D /> : fmt.devBps(d)}</div>
      <div className="q-sp q-dev-base">{fmt.bps(base) ?? <D />}</div>
    </td>
  );
}

// Котировка стакана двумя этажами в одной ячейке: чистая цена, под ней Y-IDX по
// ней же. Две колонки вместо четырёх — глаз читает пару «цена/спред» как одно
// значение, а не бегает через полтаблицы, чтобы их сопоставить.
function Quote({ px, spread, title, vwap, side }) {
  // сторона красит ячейку целиком (бид зелёным, оффер красным) — фон почти
  // прозрачный, чтобы не спорить с цветом Y-IDX под ценой
  const cls = side === "bid" ? " q-bid" : " q-ask";
  // заявки нет вовсе — один прочерк, а не два друг под другом
  if (px == null && spread == null) return <td className={"num" + cls} title={title}><D /></td>;
  return (
    <td className={"num q-cell" + cls} title={title}>
      <div className={"q-px" + (vwap ? " q-vwap" : "")}>{fmt.pct(px) ?? <D />}</div>
      <div className="q-sp" style={spread == null ? undefined : dmColor(spread)}>
        {spread == null ? <D /> : fmt.bps(spread)}</div>
    </td>
  );
}

// Подпись ячейки котировки. Без фильтра по объёму — верх стакана MOEX; с ним —
// VWAP набора тикета по лестнице Alor (объём стороны в ₽ — b._vwap_bid/_vwap_ask).
function qTitle(b, side) {
  const base = side === "bid"
    ? "лучшая заявка на покупку (MOEX BID): чистая цена и R-spread по ней (продажа в бид)"
    : "лучшая заявка на продажу (MOEX OFFER): чистая цена и R-spread по ней (покупка с оффера)";
  const vol = side === "bid" ? b._vwap_bid : b._vwap_ask;
  if (!vol) return base;
  const lv = side === "bid" ? b._vwap_bid_levels : b._vwap_ask_levels;
  const mln = fmt.num(vol / 1e6, 1);
  return `средневзвешенная цена набора ${mln} млн ₽ (грязными) по стакану`
    + (lv ? `: ${lv} ур.` : "")
    + `; R-spread пересчитан на неё (линеаризация от верха стакана)`;
}

// Маркеры оферты перед датой погашения. p и c — РАЗНЫЕ факты из разных источников,
// не взаимоисключающие: p — ближайшая будущая оферта из MOEX bondization (дата
// известна, рынок прайсит бумагу к ней); c — call-опцион эмитента из corpbonds
// (даты нет: MOEX в offertype колл не различает вовсе). У бумаги может быть и то,
// и другое → рисуем «pc». has_call === false («колла нет») и null («не знаем»)
// одинаково молчат: маркер утверждает наличие, а не отсутствие.
function OfferMarks({ b }) {
  const put = b.offer_kind === "put" && b.offer_date;
  const call = b.has_call === true || b.offer_kind === "call";
  if (!put && !call) return null;
  // ЖИРНЫЙ маркер = метрики строки посчитаны к этому горизонту (правило цены:
  // цена ниже цены пут-выкупа → к оферте, выше цены call-выкупа → к коллу).
  const hz = b.preferred_horizon;
  return (
    <>
      {put && <span className={"offer-mark offer-put" + (hz === "put" ? " offer-mark-on" : "")}
        title={"пут-оферта " + fmt.date(b.offer_date)}>p</span>}
      {call && <span className={"offer-mark offer-call" + (hz === "call" ? " offer-mark-on" : "")}
        title={b.offer_kind === "call" && b.offer_date ? "call-оферта " + fmt.date(b.offer_date) : "call-опцион"}>c</span>}
    </>
  );
}

// Каждая колонка: key (для сортировки/видимости), label/sub (шапка), align (стили шапки),
// cell(b) — полный <td>. Порядок = порядок в таблице.
// sep: true — начало блока (портфель / наша модель) → вертикальный разделитель слева.
// w — ширина колонки в ch (шрифт моноширинный): считается по МАКСИМУМУ формата
// (самое длинное возможное значение либо подпись шапки), а не по текущим данным,
// поэтому тик цены, смена фильтра или сортировки не двигают колонки. См.
// table-layout: fixed для .grid.cols-fixed в styles.css.
export const COLS = [
  // ── статика бумаги ──
  { key: "short_name", label: "INSTRUMENT", align: "left", w: 24,
    cell: (b) => {
      // ОФЗ-ПК (суверенные флоатеры) — имя MOEX «ОФЗ 29xxx»; остальное — корпораты
      const isOfz = /^\s*ОФЗ/i.test(b.short_name || "");
      return (
        <td className="left name-cell" key="short_name">
          <div className="bond-name">
            <span className={"fx-cls fx-" + (isOfz ? "ofz" : "corp")}>{isOfz ? "ОФЗ" : "КОРП"}</span>
            {b.short_name || b.isin}
            {/* рейтинг здесь же, цветом бакета (как в фильтрах) — отдельной колонки не держим */}
            {b.rating && <span className="bond-rt" style={{ color: ratingColor(b.rating) }}>({b.rating})</span>}
            {b.price_implausible && <span className="badge-stale" title="Цена подразумевает номинальный убыток (dirty > Σ будущих потоков) — вероятно стейл/тонкая цена неликвида. Спреды скрыты.">стейл</span>}
            {!b.price_implausible && b.price_thin && <span className="badge-thin" title="0 сделок сегодня на MOEX — цена несвежая (вчерашний/старый принт). DM/z сняты с ненадёжной цены.">тонк</span>}
          </div>
          <IsinCopy isin={b.isin} />
        </td>
      );
    } },
  { key: "base_rate_type", label: "BASE", w: 6,
    cell: (b) => <td key="base_rate_type"><span className={"badge " + b.base_rate_type}
      title={b.base_rate_type}>{baseLabel(b.base_rate_type)}</span></td> },
  { key: "formula", label: "FORMULA", align: "left", w: 15,
    cell: (b) => <td className="left bond-formula" key="formula">
      <CouponFormula base={b.base_rate_type} spreadBps={b.spread_issue_bps}
        couponsPerYear={b.coupons_per_year} formula={b.formula} /></td> },
  { key: "spread_issue_bps", label: "SPREAD", sub: "ISS BPS", align: "num", w: 8,
    cell: (b) => <td className="num" key="spread_issue_bps">{b.spread_issue_bps != null ? "+" + b.spread_issue_bps : <D />}</td> },
  { key: "next_coupon_date", label: "COUPON", sub: "NEXT", w: 10,
    cell: (b) => <td className="num" style={{ fontSize: 12 }} key="next_coupon_date">{fmt.date(b.next_coupon_date) ?? <D />}</td> },
  // Два этажа: погашение с годами до него, под ним — дата оферты (если есть) с
  // годами до неё, мелким и серым. Дата ГОРИЗОНТА ПРАЙСИНГА (той, к которой
  // посчитан спред строки) помечена СИНИМИ ГОДАМИ в скобках — но только когда
  // выбор реально был: без оферты и колла горизонт один, и подсветка каждой
  // строки ничего не сообщает.
  // w=17: «p 10.10.2029 (4.2)» — ширина по МАКСИМУМУ формата, иначе появление
  // маркера или второго этажа у одной бумаги дёргает колонку.
  { key: "maturity_date", label: "MATURITY", sub: "(ЛЕТ) · ОФЕРТА", w: 17,
    cell: (b) => {
      const hz = b.preferred_horizon;
      const hasChoice = !!b.offer_date || b.has_call === true;
      return (
        <td className="num mat-cell" key="maturity_date">
          <div className="mat-main">
            {/* маркеры стоят у ДАТЫ ОФЕРТЫ (второй этаж) — они про неё. У
                погашения остаются, только когда этажа нет: колл без даты
                (has_call из corpbonds) иначе потерял бы маркер вовсе */}
            {!b.offer_date && <OfferMarks b={b} />}
            {fmt.date(b.maturity_date) ?? <D />}
            {yearsTo(b.maturity_date) != null && (
              <span className={"mat-yrs" + (hasChoice && hz === "maturity" ? " mat-hz" : "")}>
                {" (" + yearsTo(b.maturity_date) + ")"}</span>
            )}
          </div>
          {b.offer_date && (
            <div className="mat-offer"
              title={(b.offer_kind === "call" ? "call-оферта " : "пут-оферта ") + fmt.date(b.offer_date)}>
              <OfferMarks b={b} />{fmt.date(b.offer_date)}
              {yearsTo(b.offer_date) != null && (
                <span className={"mat-yrs" + (hz === "put" || hz === "call" ? " mat-hz" : "")}>
                  {" (" + yearsTo(b.offer_date) + ")"}</span>
              )}
            </div>
          )}
        </td>
      );
    } },
  // ── НАША МОДЕЛЬ (стакан → последняя сделка → dirty → R-spread (первичная) → SM → DM → Z) ──
  // Верх стакана MOEX (board snapshot, TTL 120с — не WS-тик): цена и Y-IDX по ней
  // в ОДНОЙ ячейке (цена сверху, спред под ней) — две колонки вместо четырёх.
  // Сортировка колонки — по Y-IDX: цены разных бумаг между собой несравнимы,
  // спред — да. Стакан идёт ПЕРВЫМ: торгуют по нему, а last — уже история.
  { key: "y_idx_bid_bps", label: "BID", sub: "% / R-spread", align: "num", sep: true, w: 8,
    cell: (b) => <Quote key="bid" side="bid" px={b.bid_price_pct} spread={b.y_idx_bid_bps} vwap={b._vwap_bid}
      title={qTitle(b, "bid")} /> },
  { key: "y_idx_ask_bps", label: "OFFER", sub: "% / R-spread", align: "num", w: 8,
    cell: (b) => <Quote key="ask" side="ask" px={b.ask_price_pct} spread={b.y_idx_ask_bps} vwap={b._vwap_ask}
      title={qTitle(b, "ask")} /> },
  // последняя сделка и всё, что от неё производно (движение, dirty) — своя группа
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", grp: true, w: 7,
    cell: (b) => <td className={"num px-last" + (b.price_stale ? " px-stale" : "")} key="last_price_pct"
      title={b.price_stale ? "пред. закрытие MOEX — нет сделок сегодня / не в Alor-потоке" : undefined}>
      {fmt.pct(b.last_price_pct) ?? <D />}</td> },
  // Средневзвес дня. У избранного — НАШ VWAP по тикам Alor (живой, тот же, что
  // рисует слой «Средневзвес» на графике), у остальных — биржевой WAPRICE из
  // снапшота MOEX. Отсюда и подпись в title: источники разные.
  { key: "wap_price_pct", label: "СР.ВЗВЕС", sub: "CLN %", align: "num", w: 8,
    cell: (b) => <td className="num" key="wap_price_pct"
      title={b._live ? "наш VWAP по сделкам дня (live)" : "WAPRICE MOEX, средневзвес дня"}>
      {fmt.pct(b.wap_price_pct) ?? <D />}</td> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num", w: 8,
    cell: (b) => {
      const delta = b.delta_to_prev_close;
      const deltaCls = delta == null ? "" : delta >= 0 ? "pos" : "neg";
      return <td className={"num " + deltaCls} key="delta_to_prev_close">{delta == null ? <D /> : <>{fmt.signed(delta)} {delta >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty_price_rub", label: "DIRTY", sub: "RUB", align: "num", w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="dirty_price_rub">{fmt.num(b.dirty_price_rub) ?? <D />}</td> },
  // ── ликвидность: оборот сегодня и средний дневной за месяц ──
  // Обе колонки в млн ₽. VOL — VALTODAY снапшота MOEX (обновляется тактом
  // котировок), ADV — Σ денег архива часовых баров за 30 дней / число торговых
  // дней рынка. Разные источники, поэтому подписи разведены.
  { key: "val_today", label: "VOL", sub: "СЕГОДНЯ, М₽", align: "num", grp: true, w: 9,
    cell: (b) => <td className="num" key="val_today"
      title="оборот сегодня, ₽ (VALTODAY MOEX)">{fmt.mln(b.val_today) ?? <D />}</td> },
  { key: "adv_1m_rub", label: "ADV", sub: "1М, М₽", align: "num", w: 8,
    cell: (b) => <td className="num" key="adv_1m_rub"
      title={"средний дневной оборот за 30 дней, ₽ — Σ денег архива часовых баров / "
        + "число торговых дней рынка (не дней, когда торговалась эта бумага)"}>
      {fmt.mln(b.adv_1m_rub) ?? <D />}</td> },
  { key: "yield_over_index_bps", label: "R-spread", sub: "IRR−ИНДЕКС", align: "num", grp: true, w: 11,
    cell: (b) => <td className={"num" + ms(b)} key="yield_over_index_bps"><Chip value={b.yield_over_index_bps} /></td> },
  // Отклонение текущего R-spread от того уровня, по которому бумага реально
  // торговалась прошлую неделю: сверху отклонение, под ним сама база. База —
  // средневзвешенный по обороту спред часовых баров за ПРЕДЫДУЩИЕ 7 дней (без
  // сегодня), спред там — по средневзвешенной цене часа и честный as-of дня.
  { key: "y_idx_dev7_bps", label: "ОТКЛ 7Д", sub: "R-spread / БАЗА", align: "num", w: 10,
    cell: (b) => <Dev7 key="y_idx_dev7_bps" b={b} /> },
  { key: "dm_bps", label: "SM", sub: "MODEL", align: "num", grp: true, w: 7,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.dm_bps)} key="sm_bps">{fmt.bps(b.dm_bps) ?? <D />}</td> },
  { key: "disc_margin_bps", label: "DM", sub: "MODEL", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.disc_margin_bps)} key="disc_margin_bps">{fmt.bps(b.disc_margin_bps) ?? <D />}</td> },
  { key: "z_model_bps", label: "OUR Z", sub: "vs КБД", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.z_model_bps)} key="z_model_bps">{fmt.bps(b.z_model_bps) ?? <D />}</td> },
  { key: "yield_xirr_pct", label: "YTM", sub: "БОНД %", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} key="yield_xirr_pct">{b.yield_xirr_pct == null ? <D /> : fmt.pct(b.yield_xirr_pct)}</td> },
  { key: "index_yield_pct", label: "YTM", sub: "RUONIA %", align: "num", w: 7,
    cell: (b) => <td className="num" key="index_yield_pct" title="доходность роллирования RUONIA до погашения — база R-spread (общая для КС и RUONIA бумаг)">{b.index_yield_pct == null ? <D /> : fmt.pct(b.index_yield_pct)}</td> },
];

// метаданные для меню видимости (без cell-функций)
export const COL_META = COLS.map(({ key, label, sub }) => ({ key, label, sub }));
export const DEFAULT_COLS = COLS.map((c) => c.key);

// memo: WS-тик цены пересобирает массив rows, но ссылки НЕИЗМЕНИВШИХСЯ бумаг
// стабильны (App точечно клонирует только тикнувшую) — memo снимает ре-рендер
// остальных ~450 строк. Требует стабильных onOpen/onToggleStar (useCallback в App)
// и стабильного cols (useMemo ниже). Flash — CSS-анимация tr.flash (styles.css)
// вместо framer-инстанса на строку.
const BondRow = memo(function BondRow({ b, onOpen, starred, onToggleStar, cols, alertFired }) {
  const prev = useRef(b.last_price_pct);
  const reduce = useReducedMotion();
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (prev.current != null && b.last_price_pct !== prev.current && !reduce) {
      setFlash(true);
    }
    prev.current = b.last_price_pct;
  }, [b.last_price_pct, reduce]);

  const open = (e) => onOpen(b.isin, e.currentTarget);
  const toggleStar = (e) => { e.stopPropagation(); onToggleStar(b.isin); };

  return (
    <tr
      className={(alertFired ? "row-alert-fired" : "") + (flash ? " flash" : "")}
      onAnimationEnd={() => setFlash(false)}
      tabIndex={0}
      role="button"
      aria-label={`${b.short_name || b.isin} ${b.isin}, открыть карточку`}
      onClick={open}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e); } }}
    >
      <td className="star-col">
        <button className={"star" + (starred ? " on" : "")} onClick={toggleStar}
          aria-label={starred ? "Убрать из watchlist" : "В watchlist"} title="Watchlist">
          {starred ? "★" : "☆"}
        </button>
      </td>
      {/* границы блоков навешиваем ЗДЕСЬ, а не внутри cell: колонки переставляемы,
          и разделитель должен ехать с колонкой, а не быть вшит в её разметку */}
      {cols.map((c) => {
        const el = c.cell(b);
        const extra = c.sep ? "col-sep" : c.grp ? "col-grp" : "";
        return extra
          ? cloneElement(el, { className: ((el.props.className || "") + " " + extra).trim() })
          : el;
      })}
      <td className="fill-col" />
    </tr>
  );
});

export default function BondTable({ rows, status, errMsg, sort, onSort, onOpen, watch = [], onToggleStar, filtered, onClearFilters, onRetry, visibleCols, onMoveCol, colWidths = {}, onResizeCol, onResetColWidth }) {
  // ПОРЯДОК КОЛОНОК = порядок visibleCols (его задаёт пользователь перетаскиванием),
  // а не порядок объявления COLS. useMemo — стабильная ссылка для memo(BondRow).
  const cols = useMemo(() => {
    const byKey = new Map(COLS.map((c) => [c.key, c]));
    const keys = visibleCols?.length ? visibleCols : DEFAULT_COLS;
    const out = keys.map((k) => byKey.get(k)).filter(Boolean);
    return out.length ? out : COLS;   // пустой/битый набор → дефолт, а не голая таблица
  }, [visibleCols]);
  // какую колонку тащим и над какой висим — для подсветки цели (ref — источник
  // правды в обработчиках, state только для стилей)
  const dragRef = useRef(null);
  const [dragKey, setDragKey] = useState(null);
  const [overKey, setOverKey] = useState(null);
  // O(1) вместо watch.includes на каждую строку
  const watchSet = useMemo(() => new Set(watch), [watch]);
  // isin'ы со сработавшим алертом → красная строка (общий кэш ['alerts'])
  const alertsQ = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts, refetchInterval: 8000 });
  const firedIsins = useMemo(
    () => new Set((alertsQ.data || []).filter((a) => a.status === "fired").map((a) => a.isin)),
    [alertsQ.data]);
  const ncols = cols.length + 2; // + star + фиктивная колонка-филлер

  let body;
  if (status === "loading") body = <tr><td colSpan={ncols} className="loading">ЗАГРУЗКА ДАННЫХ</td></tr>;
  else if (status === "error") body = (
    <tr><td colSpan={ncols} className="empty">
      <div className="empty-msg">Ошибка — {errMsg}</div>
      <button className="btn" onClick={onRetry}>Повторить</button>
    </td></tr>
  );
  else if (!rows.length) body = (
    <tr><td colSpan={ncols} className="empty">
      <div className="empty-msg">{filtered ? "Ничего не найдено по фильтру" : "Нет инструментов"}</div>
      {filtered && <button className="btn" onClick={onClearFilters}>Сбросить фильтр</button>}
    </td></tr>
  );
  else body = rows.map((b) => (
    <BondRow key={b.isin} b={b} onOpen={onOpen} starred={watchSet.has(b.isin)} onToggleStar={onToggleStar} cols={cols} alertFired={firedIsins.has(b.isin)} />
  ));

  return (
    <section className="table-wrap">
      <table className="grid packed cols-fixed">
        <colgroup>
          <col className="col-star" />
          {/* натянутая мышью ширина (px) перебивает дефолтную из COLS.w */}
          {cols.map((c) => <col key={c.key} style={colWidths[c.key]
            ? { width: colWidths[c.key] + "px" }
            : { "--cw": (c.w || 8) + "ch" }} />)}
          <col className="col-fill" />
        </colgroup>
        <thead>
          <tr>
            <th className="star-col" aria-label="Watchlist" />
            {cols.map((c) => <HeaderCell key={c.key} col={c} sort={sort} onSort={onSort}
              onMoveCol={onMoveCol} dragRef={dragRef} dragKey={dragKey} setDragKey={setDragKey}
              overKey={overKey} setOverKey={setOverKey}
              onResizeCol={onResizeCol} onResetColWidth={onResetColWidth} />)}
            <th className="fill-col" aria-hidden="true" />
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </section>
  );
}
