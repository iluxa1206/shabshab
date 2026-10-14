import { fmt } from "../format.js";
import { COLS as FLOATER_COLS, D } from "./BondTable.jsx";
import { FIXED_COLS } from "./fixed/fixedCols.jsx";
import { durValue } from "../unifiedRows.js";

// КОЛОНКИ ОБЩЕГО МОНИТОРА ОБЛИГАЦИЙ (docs/unified_monitor_tz.md, этап B).
//
// Ничего не пересчитываем и не рисуем заново: объединённая колонка вызывает
// ЯЧЕЙКУ флоатера или фикса по row.kind — обе давно отлажены, и формат ячейки
// у класса остаётся таким, каким его привыкли читать. Колонка, которой у класса
// нет, даёт ПУСТУЮ ячейку (прочерк значит «должно быть, но данных нет» — это
// другое). Первая ячейка строки несёт цветную полосу класса (CSS .uni).
//
// У колонки `only` — класс, которому она принадлежит: меню столбцов группирует
// по нему, а таблица скрывает колонку, когда этого класса в выборке нет.

const byKey = (cols) => Object.fromEntries(cols.map((c) => [c.key, c]));
const FL = byKey(FLOATER_COLS);
const FX = byKey(FIXED_COLS);

const empty = (key) => <td className="num" key={key} />;

/** Ячейка по классу: флоатер — cell из BondTable.COLS, фикс — из fixedCols. */
const pick = (flKey, fxKey = flKey) => (b) => {
  const c = b.kind === "fixed" ? FX[fxKey] : FL[flKey];
  return c ? c.cell(b) : empty(flKey);
};

/** Колонка, у которой есть данные только у одного класса. */
const onlyFor = (kind, col) => ({
  ...col, only: kind,
  cell: (b) => (b.kind === kind ? col.cell(b) : empty(col.key)),
});

// Подписи единицы в DUR: у фикса дюрация, у флоатера срок до горизонта — одна
// колонка по решению владельца (14.10.2026), поэтому подпись в самой ячейке.
const DUR = {
  key: "dur", label: "DUR", sub: "ЛЕТ", align: "num", grp: true, w: 8,
  title: "Фикс — модифицированная дюрация, лет. Флоатер — срок до горизонта прайсинга "
         + "(оферта по правилу цены, иначе погашение), лет: у плавающего купона "
         + "дюрация к ставке близка к нулю, и срок информативнее.",
  cell: (b) => {
    const v = durValue(b);
    return (
      <td className={"num" + (b._mstale ? " mstale" : "")} key="dur"
        title={b.kind === "fixed" ? "модифицированная дюрация, лет" : "лет до горизонта прайсинга"}>
        {v == null ? <D /> : fmt.num(v, 2)}
        {b.kind !== "fixed" && v != null && <span className="dur-hz" aria-hidden="true">·срок</span>}
      </td>
    );
  },
};

// КУПОН: формула у флоатера, ставка у фикса. Сортировка — по ставке/марже.
const COUPON = {
  key: "coupon", label: "КУПОН", sub: "ФОРМУЛА / СТАВКА", align: "left", w: 15,
  title: "Флоатер — формула купона (база + маржа). Фикс — ставка текущего купона, %.",
  cell: (b) => (b.kind === "fixed" ? FX.coupon_pct.cell(b) : FL.formula.cell(b)),
};

// INSTRUMENT: полоса класса вешается тут же — на первую ячейку строки.
const INSTRUMENT = {
  ...FL.short_name,
  cell: (b) => {
    const el = b.kind === "fixed" ? FX.name.cell(b) : FL.short_name.cell(b);
    const cls = "cls-" + (b.kind === "fixed" ? "fixed" : "float");
    return { ...el, props: { ...el.props, className: ((el.props.className || "") + " " + cls).trim() } };
  },
};

// Колонки, общие по смыслу, но с разными ключами и полями у классов.
const BID = { ...FL.y_idx_bid_bps, label: "BID", sub: "% / SPREAD / YTM", cell: pick("y_idx_bid_bps", "ytm_bid") };
const ASK = { ...FL.y_idx_ask_bps, label: "ASK", sub: "% / SPREAD / YTM", cell: pick("y_idx_ask_bps", "ytm_ask") };
const WAP = { ...FL.wap_price_pct, cell: pick("wap_price_pct", "ytm_wap") };
const YTM = {
  key: "yield_xirr_pct", label: "YTM", sub: "К ПОГАШ. %", align: "num", grp: true, w: 8,
  title: "Фикс — доходность к погашению (к оферте, если поток обрезан на ней). "
         + "Флоатер — модельная доходность: купоны по форварду индекса (курсивом).",
  cell: (b) => {
    const v = b.kind === "fixed" ? b.ytm : b.yield_xirr_pct;
    return (
      <td className={"num" + (b._mstale ? " mstale" : "") + (b.kind === "fixed" ? "" : " ytm-model")}
        key="yield_xirr_pct"
        title={b.kind === "fixed" ? undefined : "модельная: купоны плавающей ставки по форварду индекса"}>
        {v == null ? <D /> : fmt.pct(v)}
      </td>
    );
  },
};
const DIRTY = { ...FL.dirty_price_rub, key: "dirty", sub: "НОМИНАЛ", cell: pick("dirty_price_rub", "dirty") };
const SPREAD = { ...FL.yield_over_index_bps, sub: "IRR−RUONIA",
  cell: (b) => (b.kind === "fixed" ? FX.yield_over_index_bps.cell(b) : FL.yield_over_index_bps.cell(b)) };

// Порядок = порядок по умолчанию: ядро, общее у обоих классов.
const COMMON = [
  INSTRUMENT,
  { ...FL.base_rate_type, label: "КЛАСС", sub: "КС / RU / ФИКС", w: 7,
    cell: (b) => (b.kind === "fixed"
      ? <td key="base_rate_type"><span className="badge FIXED" title="Фиксированный купон">ФИКС</span></td>
      : FL.base_rate_type.cell(b)) },
  COUPON,
  FL.maturity_date && { ...FL.maturity_date, cell: pick("maturity_date") },
  BID, ASK, FL.last_price_pct, WAP, FL.delta_to_prev_close,
  SPREAD, YTM, DIRTY, FL.val_today, FL.adv_1m_rub, DUR,
  FL.issue_date, FL.issue_volume,
].filter(Boolean);

// Только флоатеры: маржа выпуска, дата купона, SM/DM/Z, база-индекс.
const FLOATER_ONLY = ["spread_issue_bps", "next_coupon_date", "dm_bps", "disc_margin_bps",
                      "z_model_bps", "index_yield_pct"].map((k) => onlyFor("floater", FL[k]));
// Только фиксы: то, чего нет у флоатера.
const FIXED_ONLY = ["delta_ytm", "cur_yield", "g_spread_bps", "z_spread_bps", "convexity", "dv01"]
  .map((k) => onlyFor("fixed", FX[k]));

export const UNIFIED_COLS = [...COMMON, ...FLOATER_ONLY, ...FIXED_ONLY];

// По умолчанию (решение владельца 16.10): ядро без PRICE/CHG/DIRTY/ISSUE/ОБЪЁМ и Δ YTM
// в конце; остальные колонки — в меню. Порядок = порядок показа.
export const UNIFIED_DEFAULT_COLS = [
  "short_name", "base_rate_type", "coupon", "maturity_date", "y_idx_bid_bps", "y_idx_ask_bps",
  "wap_price_pct", "yield_over_index_bps", "yield_xirr_pct", "val_today", "adv_1m_rub",
  "dur", "delta_ytm",
];

// Метаданные меню с группой: общие / флоатеры / фиксы.
export const UNIFIED_COL_META = UNIFIED_COLS.map(({ key, label, sub, only }) => (
  { key, label, sub, group: only || "common" }));
