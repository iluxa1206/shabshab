import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";
import { fmt, dmColor } from "../format.js";

const D = () => <span className="dash">—</span>;

// Δz: расширение спреда (>0) = цена упала = красный; сужение = зелёный
const dzColor = (v) => (v == null ? { color: "var(--mut-2)" } : { color: v > 0 ? "var(--down)" : "var(--up)" });

function Chip({ value }) {
  if (value == null) return <D />;
  return <span className="dm-chip" style={dmColor(value)}>{fmt.bps(value)} {value >= 0 ? "▲" : "▼"}</span>;
}

// перцентиль z внутри рейтинг-бакета: мини-бар + число (высокий = дёшево к группе)
function PctileBar({ p }) {
  return (
    <span className="pctile" title={`${p}-й перцентиль z в рейтинг-бакете`}>
      <span className="pctile-track"><span className="pctile-fill" style={{ width: p + "%" }} /></span>
      <span className="pctile-num">{p}</span>
    </span>
  );
}

// Каждая колонка: key (для сортировки/видимости), label/sub (шапка), align/nrd (стили шапки),
// cell(b) — полный <td>. Порядок = порядок в таблице.
// sep: true — начало блока (портфель / наша модель / НРД) → вертикальный разделитель слева.
export const COLS = [
  // ── статика бумаги ──
  { key: "short_name", label: "INSTRUMENT", align: "left",
    cell: (b) => (
      <td className="left" key="short_name">
        <div className="bond-name">{b.short_name || b.isin}</div>
        <div className="mono muted" style={{ fontSize: 11 }}>{b.isin}</div>
      </td>
    ) },
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
  // ── НАША МОДЕЛЬ (цена → CHG → dirty → DM → Z → carry → z%ile → Δz) ──
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", sep: true,
    cell: (b) => <td className="num col-sep" key="last_price_pct">{fmt.pct(b.last_price_pct) ?? <D />}</td> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num",
    cell: (b) => {
      const delta = b.delta_to_prev_close;
      const deltaCls = delta == null ? "" : delta >= 0 ? "pos" : "neg";
      return <td className={"num " + deltaCls} key="delta_to_prev_close">{delta == null ? <D /> : <>{fmt.signed(delta)} {delta >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty_price_rub", label: "DIRTY", sub: "RUB", align: "num",
    cell: (b) => <td className="num" key="dirty_price_rub">{fmt.num(b.dirty_price_rub) ?? <D />}</td> },
  { key: "sm_bps", label: "SM", sub: "MODEL", align: "num",
    cell: (b) => <td className="num" key="sm_bps"><Chip value={b.dm_bps} /></td> },
  { key: "disc_margin_bps", label: "DM", sub: "MODEL", align: "num",
    cell: (b) => <td className="num" key="disc_margin_bps"><Chip value={b.disc_margin_bps} /></td> },
  { key: "z_model_bps", label: "OUR Z", sub: "vs КБД", align: "num",
    cell: (b) => <td className="num" style={dmColor(b.z_model_bps)} key="z_model_bps">{fmt.bps(b.z_model_bps) ?? <D />}</td> },
  { key: "carry_bps", label: "CARRY", sub: "vs БАЗА", align: "num",
    cell: (b) => <td className="num" style={b.carry_bps != null ? dmColor(b.carry_bps) : undefined} key="carry_bps">{b.carry_bps == null ? <D /> : fmt.bps(b.carry_bps)}</td> },
  { key: "z_pctile", label: "z%ile", sub: "RATING", align: "num",
    cell: (b) => <td className="num" key="z_pctile">{b.z_pctile == null ? <D /> : <PctileBar p={b.z_pctile} />}</td> },
  { key: "delta_z_dod", label: "Δz", sub: "D/D BPS", align: "num",
    cell: (b) => <td className="num" style={dzColor(b.delta_z_dod)} key="delta_z_dod">{b.delta_z_dod == null ? <D /> : fmt.bps(b.delta_z_dod)}</td> },
  { key: "delta_z_mom", label: "Δz", sub: "M/M BPS", align: "num",
    cell: (b) => <td className="num" style={dzColor(b.delta_z_mom)} key="delta_z_mom">{b.delta_z_mom == null ? <D /> : fmt.bps(b.delta_z_mom)}</td> },
  // ── НРД (тот же порядок: цена → SM → DM → Z; наш SM↔НРД simple, наш DM↔НРД discount) ──
  { key: "nrd_price_pct", label: "NRD PX", sub: "CLN %", align: "num", nrd: true, sep: true,
    cell: (b) => <td className="num nrd-col col-sep" key="nrd_price_pct">{fmt.pct(b.nrd_price_pct) ?? <D />}</td> },
  { key: "simple_margin_bps", label: "NRD SM", sub: "BPS", align: "num", nrd: true,
    cell: (b) => <td className="num nrd-col" key="simple_margin_bps"><Chip value={b.simple_margin_bps} /></td> },
  { key: "discount_margin_bps", label: "NRD DM", sub: "BPS", align: "num", nrd: true,
    cell: (b) => <td className="num nrd-col" key="discount_margin_bps"><Chip value={b.discount_margin_bps} /></td> },
  { key: "z_spread_bps", label: "NRD Z", sub: "SPRD", align: "num", nrd: true,
    cell: (b) => <td className="num nrd-col" style={dmColor(b.z_spread_bps)} key="z_spread_bps">{fmt.bps(b.z_spread_bps) ?? <D />}</td> },
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
    (col.nrd ? "nrd-col " : "") +
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
