import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { baseLabel, fmt, dmColor } from "../format.js";
import { fetchAlerts } from "../api.js";
import CouponFormula from "./CouponFormula.jsx";

const D = () => <span className="dash">—</span>;

// WS тикнул цену, но производные метрики (DM/SM/z/dirty/CHG/Y-IDX) ещё
// от прошлого расчёта бэка → dim-класс, чтобы трейдер не читал их как актуальные.
const ms = (b) => (b._mstale ? " mstale" : "");

// mid-чип (жирный, со стрелкой) — первичная метрика; sub-чип (bid/offer) без
// стрелки и приглушён: три стрелки подряд рябят, а доминировать должен mid.
function Chip({ value, sub }) {
  if (value == null) return <D />;
  return <span className={"dm-chip" + (sub ? " sub" : "")} style={dmColor(value)}>
    {fmt.bps(value)}{sub ? "" : ` ${value >= 0 ? "▲" : "▼"}`}</span>;
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
  { key: "short_name", label: "INSTRUMENT", align: "left", w: 17,
    cell: (b) => {
      // ОФЗ-ПК (суверенные флоатеры) — имя MOEX «ОФЗ 29xxx»; остальное — корпораты
      const isOfz = /^\s*ОФЗ/i.test(b.short_name || "");
      return (
        <td className="left" key="short_name">
          <div className="bond-name">
            <span className={"fx-cls fx-" + (isOfz ? "ofz" : "corp")}>{isOfz ? "ОФЗ" : "КОРП"}</span>
            {b.short_name || b.isin}
            {b.price_implausible && <span className="badge-stale" title="Цена подразумевает номинальный убыток (dirty > Σ будущих потоков) — вероятно стейл/тонкая цена неликвида. Спреды скрыты.">стейл</span>}
            {!b.price_implausible && b.price_thin && <span className="badge-thin" title="0 сделок сегодня на MOEX — цена несвежая (вчерашний/старый принт). DM/z сняты с ненадёжной цены.">тонк</span>}
          </div>
        </td>
      );
    } },
  { key: "base_rate_type", label: "BASE", w: 6,
    cell: (b) => <td key="base_rate_type"><span className={"badge " + b.base_rate_type}
      title={b.base_rate_type}>{baseLabel(b.base_rate_type)}</span></td> },
  { key: "rating", label: "RATING", w: 7,
    cell: (b) => <td className="rating-cell" key="rating">{b.rating || <D />}</td> },
  { key: "formula", label: "FORMULA", align: "left", w: 17,
    cell: (b) => <td className="left bond-formula" key="formula">
      <CouponFormula base={b.base_rate_type} spreadBps={b.spread_issue_bps}
        couponsPerYear={b.coupons_per_year} formula={b.formula} /></td> },
  { key: "spread_issue_bps", label: "SPREAD", sub: "ISS BPS", align: "num", w: 8,
    cell: (b) => <td className="num" key="spread_issue_bps">{b.spread_issue_bps != null ? "+" + b.spread_issue_bps : <D />}</td> },
  { key: "next_coupon_date", label: "COUPON", sub: "NEXT", w: 10,
    cell: (b) => <td className="num" style={{ fontSize: 12 }} key="next_coupon_date">{fmt.date(b.next_coupon_date) ?? <D />}</td> },
  { key: "maturity_date", label: "MATURITY", w: 10,
    cell: (b) => <td className="num" style={{ fontSize: 12 }} key="maturity_date">{fmt.date(b.maturity_date) ?? <D />}</td> },
  // ── НАША МОДЕЛЬ (цена → CHG → dirty → Y−IDX (первичная) → SM → DM → Z) ──
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", sep: true, w: 7,
    cell: (b) => <td className={"num col-sep px-last" + (b.price_stale ? " px-stale" : "")} key="last_price_pct"
      title={b.price_stale ? "пред. закрытие MOEX — нет сделок сегодня / не в Alor-потоке" : undefined}>
      {fmt.pct(b.last_price_pct) ?? <D />}</td> },
  // верх стакана MOEX (board snapshot, TTL 120с — не WS-тик): цена покупки/продажи.
  // grp — мягкий разделитель группы: PRICE-якорь отделён от котировок стакана.
  { key: "bid_price_pct", label: "BID", sub: "CLN %", align: "num", grp: true, w: 7,
    cell: (b) => <td className="num col-grp px-quote" key="bid_price_pct"
      title="лучшая заявка на покупку (MOEX BID), чистая цена">{fmt.pct(b.bid_price_pct) ?? <D />}</td> },
  { key: "ask_price_pct", label: "OFFER", sub: "CLN %", align: "num", w: 7,
    cell: (b) => <td className="num px-quote" key="ask_price_pct"
      title="лучшая заявка на продажу (MOEX OFFER), чистая цена">{fmt.pct(b.ask_price_pct) ?? <D />}</td> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num", grp: true, w: 8,
    cell: (b) => {
      const delta = b.delta_to_prev_close;
      const deltaCls = delta == null ? "" : delta >= 0 ? "pos" : "neg";
      return <td className={"num col-grp " + deltaCls} key="delta_to_prev_close">{delta == null ? <D /> : <>{fmt.signed(delta)} {delta >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty_price_rub", label: "DIRTY", sub: "RUB", align: "num", w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="dirty_price_rub">{fmt.num(b.dirty_price_rub) ?? <D />}</td> },
  { key: "yield_over_index_bps", label: "Y−IDX", sub: "IRR−ИНДЕКС", align: "num", grp: true, w: 11,
    cell: (b) => <td className={"num col-grp" + ms(b)} key="yield_over_index_bps"><Chip value={b.yield_over_index_bps} /></td> },
  // Y-IDX по верху стакана: продажа по bid / покупка по offer — граница сделки,
  // а не по последнему принту. Считаются на том же потоке, что mid-Y-IDX.
  { key: "y_idx_bid_bps", label: "Y−IDX", sub: "BID", align: "num", w: 9,
    cell: (b) => <td className="num" key="y_idx_bid_bps"
      title="Y-IDX по цене лучшей покупки (продажа в бид)"><Chip value={b.y_idx_bid_bps} sub /></td> },
  { key: "y_idx_ask_bps", label: "Y−IDX", sub: "OFFER", align: "num", w: 9,
    cell: (b) => <td className="num" key="y_idx_ask_bps"
      title="Y-IDX по цене лучшей продажи (покупка с оффера)"><Chip value={b.y_idx_ask_bps} sub /></td> },
  { key: "dm_bps", label: "SM", sub: "MODEL", align: "num", grp: true, w: 7,
    cell: (b) => <td className={"num col-grp" + ms(b)} style={dmColor(b.dm_bps)} key="sm_bps">{fmt.bps(b.dm_bps) ?? <D />}</td> },
  { key: "disc_margin_bps", label: "DM", sub: "MODEL", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.disc_margin_bps)} key="disc_margin_bps">{fmt.bps(b.disc_margin_bps) ?? <D />}</td> },
  { key: "z_model_bps", label: "OUR Z", sub: "vs КБД", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.z_model_bps)} key="z_model_bps">{fmt.bps(b.z_model_bps) ?? <D />}</td> },
  { key: "yield_xirr_pct", label: "YTM", sub: "БОНД %", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} key="yield_xirr_pct">{b.yield_xirr_pct == null ? <D /> : fmt.pct(b.yield_xirr_pct)}</td> },
  { key: "index_yield_pct", label: "YTM", sub: "RUONIA %", align: "num", w: 7,
    cell: (b) => <td className="num" key="index_yield_pct" title="доходность роллирования RUONIA до погашения — база Y-IDX (общая для КС и RUONIA бумаг)">{b.index_yield_pct == null ? <D /> : fmt.pct(b.index_yield_pct)}</td> },
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
      {cols.map((c) => c.cell(b))}
      <td className="fill-col" />
    </tr>
  );
});

function HeaderCell({ col, sort, onSort }) {
  const active = sort.key === col.key;
  const cls =
    (col.align === "left" ? "left " : col.align === "num" ? "num " : "") +
    (col.sep ? "col-sep " : "") + (col.grp ? "col-grp " : "") +
    (active ? "sorted " + (sort.dir === "asc" ? "asc" : "") : "");
  return (
    <th
      className={cls.trim()}
      role="button"
      tabIndex={0}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
      onClick={() => onSort(col.key)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSort(col.key); } }}
    >
      {col.label}{col.sub && <><br /><small>{col.sub}</small></>}
    </th>
  );
}

export default function BondTable({ rows, status, errMsg, sort, onSort, onOpen, watch = [], onToggleStar, filtered, onClearFilters, onRetry, visibleCols }) {
  // видимые колонки в исходном порядке COLS; useMemo — стабильная ссылка для memo(BondRow)
  const cols = useMemo(() => {
    const visSet = new Set(visibleCols?.length ? visibleCols : DEFAULT_COLS);
    return COLS.filter((c) => visSet.has(c.key));
  }, [visibleCols]);
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
          {cols.map((c) => <col key={c.key} style={{ "--cw": (c.w || 8) + "ch" }} />)}
          <col className="col-fill" />
        </colgroup>
        <thead>
          <tr>
            <th className="star-col" aria-label="Watchlist" />
            {cols.map((c) => <HeaderCell key={c.key} col={c} sort={sort} onSort={onSort} />)}
            <th className="fill-col" aria-hidden="true" />
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </section>
  );
}
