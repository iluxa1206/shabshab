import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";
import { fmt, dmColor } from "../format.js";

const D = () => <span className="dash">—</span>;

// WS тикнул цену, но производные метрики (DM/SM/z/carry/dirty/CHG/Y-IDX) ещё
// от прошлого расчёта бэка → dim-класс, чтобы трейдер не читал их как актуальные.
const ms = (b) => (b._mstale ? " mstale" : "");

function Chip({ value }) {
  if (value == null) return <D />;
  return <span className="dm-chip" style={dmColor(value)}>{fmt.bps(value)} {value >= 0 ? "▲" : "▼"}</span>;
}

// Каждая колонка: key (для сортировки/видимости), label/sub (шапка), align (стили шапки),
// cell(b) — полный <td>. Порядок = порядок в таблице.
// sep: true — начало блока (портфель / наша модель) → вертикальный разделитель слева.
export const COLS = [
  // ── статика бумаги ──
  { key: "short_name", label: "INSTRUMENT", align: "left",
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
  { key: "base_rate_type", label: "BASE",
    cell: (b) => <td key="base_rate_type"><span className={"badge " + b.base_rate_type}>{b.base_rate_type}</span></td> },
  { key: "rating", label: "RATING",
    cell: (b) => <td className="rating-cell" key="rating">{b.rating || <D />}</td> },
  { key: "formula", label: "FORMULA", align: "left",
    cell: (b) => <td className="left bond-formula" key="formula">{b.formula || "—"}</td> },
  { key: "spread_issue_bps", label: "SPREAD", sub: "ISS BPS", align: "num",
    cell: (b) => <td className="num" key="spread_issue_bps">{b.spread_issue_bps != null ? "+" + b.spread_issue_bps : <D />}</td> },
  { key: "next_coupon_date", label: "COUPON", sub: "NEXT",
    cell: (b) => <td className="num" style={{ fontSize: 12 }} key="next_coupon_date">{fmt.date(b.next_coupon_date) ?? <D />}</td> },
  { key: "maturity_date", label: "MATURITY",
    cell: (b) => <td className="num" style={{ fontSize: 12 }} key="maturity_date">{fmt.date(b.maturity_date) ?? <D />}</td> },
  // ── портфель ──
  { key: "qty", label: "ПОЗИЦИЯ", sub: "ШТ", align: "num", portfolio: true, sep: true,
    cell: (b) => <td className="num col-sep" key="qty">{b.qty == null ? <D /> : fmt.num(b.qty, 0)}</td> },
  { key: "pos_value", label: "СТОИМОСТЬ", sub: "RUB", align: "num", portfolio: true,
    cell: (b) => <td className="num" key="pos_value">{b.pos_value == null ? <D /> : fmt.num(b.pos_value, 0)}</td> },
  // ── НАША МОДЕЛЬ (цена → CHG → dirty → SM → DM → Z → carry → Y−IDX) ──
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", sep: true,
    cell: (b) => <td className={"num col-sep" + (b.price_stale ? " px-stale" : "")} key="last_price_pct"
      title={b.price_stale ? "пред. закрытие MOEX — нет сделок сегодня / не в Alor-потоке" : undefined}>
      {fmt.pct(b.last_price_pct) ?? <D />}</td> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num",
    cell: (b) => {
      const delta = b.delta_to_prev_close;
      const deltaCls = delta == null ? "" : delta >= 0 ? "pos" : "neg";
      return <td className={"num " + deltaCls} key="delta_to_prev_close">{delta == null ? <D /> : <>{fmt.signed(delta)} {delta >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty_price_rub", label: "DIRTY", sub: "RUB", align: "num",
    cell: (b) => <td className={"num" + ms(b)} key="dirty_price_rub">{fmt.num(b.dirty_price_rub) ?? <D />}</td> },
  { key: "dm_bps", label: "SM", sub: "MODEL", align: "num",
    cell: (b) => <td className={"num" + ms(b)} key="sm_bps"><Chip value={b.dm_bps} /></td> },
  { key: "disc_margin_bps", label: "DM", sub: "MODEL", align: "num",
    cell: (b) => <td className={"num" + ms(b)} key="disc_margin_bps"><Chip value={b.disc_margin_bps} /></td> },
  { key: "z_model_bps", label: "OUR Z", sub: "vs КБД", align: "num",
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.z_model_bps)} key="z_model_bps">{fmt.bps(b.z_model_bps) ?? <D />}</td> },
  { key: "carry_bps", label: "CARRY", sub: "vs БАЗА", align: "num",
    cell: (b) => <td className={"num" + ms(b)} style={b.carry_bps != null ? dmColor(b.carry_bps) : undefined} key="carry_bps">{b.carry_bps == null ? <D /> : fmt.bps(b.carry_bps)}</td> },
  { key: "yield_over_index_bps", label: "Y−IDX", sub: "IRR−ИНДЕКС", align: "num",
    cell: (b) => <td className={"num" + ms(b)} style={b.yield_over_index_bps != null ? dmColor(b.yield_over_index_bps) : undefined} key="yield_over_index_bps">{b.yield_over_index_bps == null ? <D /> : fmt.bps(b.yield_over_index_bps)}</td> },
  { key: "yield_xirr_pct", label: "YTM", sub: "БОНД %", align: "num",
    cell: (b) => <td className={"num" + ms(b)} key="yield_xirr_pct">{b.yield_xirr_pct == null ? <D /> : fmt.pct(b.yield_xirr_pct)}</td> },
  { key: "index_yield_pct", label: "YTM", sub: "БАЗА %", align: "num",
    cell: (b) => <td className="num" key="index_yield_pct">{b.index_yield_pct == null ? <D /> : fmt.pct(b.index_yield_pct)}</td> },
];

// метаданные для меню видимости (без cell-функций)
export const COL_META = COLS.map(({ key, label, sub, portfolio }) => ({ key, label, sub, portfolio }));
export const DEFAULT_COLS = COLS.filter((c) => !c.portfolio).map((c) => c.key); // портфельные скрыты по умолчанию

// memo: WS-тик цены пересобирает массив rows, но ссылки НЕИЗМЕНИВШИХСЯ бумаг
// стабильны (App точечно клонирует только тикнувшую) — memo снимает ре-рендер
// остальных ~450 строк. Требует стабильных onOpen/onToggleStar (useCallback в App)
// и стабильного cols (useMemo ниже). Flash — CSS-анимация tr.flash (styles.css)
// вместо framer-инстанса на строку.
const BondRow = memo(function BondRow({ b, onOpen, starred, onToggleStar, cols }) {
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
      className={flash ? "flash" : undefined}
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
    </tr>
  );
});

function HeaderCell({ col, sort, onSort }) {
  const active = sort.key === col.key;
  const cls =
    (col.align === "left" ? "left " : col.align === "num" ? "num " : "") +
    (col.sep ? "col-sep " : "") +
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
  const ncols = cols.length + 1; // + star

  let body;
  if (status === "loading") body = <tr><td colSpan={ncols} className="loading">LOADING MARKET DATA</td></tr>;
  else if (status === "error") body = (
    <tr><td colSpan={ncols} className="empty">
      <div className="empty-msg">ERROR — {errMsg}</div>
      <button className="btn" onClick={onRetry}>Retry</button>
    </td></tr>
  );
  else if (!rows.length) body = (
    <tr><td colSpan={ncols} className="empty">
      <div className="empty-msg">{filtered ? "Ничего не найдено по фильтру" : "Нет инструментов"}</div>
      {filtered && <button className="btn" onClick={onClearFilters}>Сбросить фильтр</button>}
    </td></tr>
  );
  else body = rows.map((b) => (
    <BondRow key={b.isin} b={b} onOpen={onOpen} starred={watchSet.has(b.isin)} onToggleStar={onToggleStar} cols={cols} />
  ));

  return (
    <section className="table-wrap">
      <table className="grid">
        <thead>
          <tr>
            <th className="star-col" aria-label="Watchlist" />
            {cols.map((c) => <HeaderCell key={c.key} col={c} sort={sort} onSort={onSort} />)}
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </section>
  );
}
