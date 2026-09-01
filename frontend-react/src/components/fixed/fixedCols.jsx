import { fmt, dmColor, ratingColor, yearsTo } from "../../format.js";
import { D, IsinCopy, Quote } from "../BondTable.jsx";

// Колонки МОНИТОРА ФИКСОВ. Формат тот же, что у флоатеров (см. BondTable.COLS):
// key — ключ сортировки и видимости, w — ширина в ch по МАКСИМУМУ формата,
// sep/grp — вертикальные разделители блоков, cell(b) — полный <td>.
//
// Первичных метрик здесь ДВЕ равноправные: g-спред (к КБД ОФЗ) и доходность к
// погашению. Цветом красим спред — у него есть осмысленный знак; YTM всегда
// положительна, окраска по знаку ничего не сообщала бы.
// WS тикнул цену, а производные (YTM/спреды/dirty) ещё от прошлого расчёта
// движка → dim-класс, чтобы трейдер не читал их как актуальные. То же правило
// и тот же класс, что у монитора флоатеров (BondTable).
const ms = (b) => (b._mstale ? " mstale" : "");

// Подпись котировки. Без фильтра по объёму — верх стакана MOEX; с ним — VWAP
// набора тикета по лестнице Alor (объём стороны в ₽ — b._vwap_bid/_vwap_ask).
function qTitle(b, side) {
  const base = side === "bid"
    ? "лучшая заявка на покупку (MOEX BID): чистая цена и g-спред по ней (продажа в бид)"
    : "лучшая заявка на продажу (MOEX OFFER): чистая цена и g-спред по ней (покупка с оффера)";
  const vol = side === "bid" ? b._vwap_bid : b._vwap_ask;
  if (!vol) return base;
  const lv = side === "bid" ? b._vwap_bid_levels : b._vwap_ask_levels;
  return `средневзвешенная цена набора ${fmt.num(vol / 1e6, 1)} млн ₽ (грязными) по стакану`
    + (lv ? `: ${lv} ур.` : "")
    + "; g-спред посчитан к ней по методике (движок метрик, такт ≤5 с)";
}

export const FIXED_COLS = [
  // ── статика бумаги ──
  { key: "name", label: "INSTRUMENT (Э/А)", align: "left", w: 24,
    cell: (b) => (
      <td className="left name-cell" key="name">
        <div className="bond-name">
          <span className={"fx-cls fx-" + b.cls}>{b.cls === "ofz" ? "ОФЗ" : "КОРП"}</span>
          {b.name || b.isin}
          {b.rating && <span className="bond-rt" style={{ color: ratingColor(b.rating) }}>({b.rating})</span>}
          {b.price_thin && <span className="badge-thin"
            title="Последняя цена MOEX старше 4 дней — бумага не торговалась, YTM и спред сняты с несвежего принта.">тонк</span>}
        </div>
        <div className="isin-row">
          <IsinCopy isin={b.isin} />
          {/* Эксперт РА / АКРА; рядом с именем — худшая из них, по ней фильтр */}
          {b.ratings_ea && (
            <span className="isin-rt" title="Эксперт РА / АКРА">({b.ratings_ea})</span>
          )}
        </div>
      </td>
    ) },
  { key: "coupon_pct", label: "COUPON", sub: "%", align: "num", w: 7,
    cell: (b) => <td className="num" key="coupon_pct">{fmt.pct(b.coupon_pct) ?? <D />}</td> },
  // Погашение с годами до него; под ним — дата оферты (поток обрезан на ней,
  // метрики строки посчитаны к выкупу по номиналу — yield-to-put).
  { key: "maturity_date", label: "MATURITY", sub: "(ЛЕТ) · ОФЕРТА", w: 17,
    cell: (b) => (
      <td className="num mat-cell" key="maturity_date">
        <div className="mat-main">
          {fmt.date(b.maturity_date) ?? <D />}
          {yearsTo(b.maturity_date) != null && (
            <span className={"mat-yrs" + (b.put_date ? "" : " mat-hz")}>
              {" (" + yearsTo(b.maturity_date) + ")"}</span>
          )}
        </div>
        {b.put_date && (
          <div className="mat-offer" title={"оферта " + fmt.date(b.put_date) + " — метрики строки посчитаны к ней"}>
            <span className="offer-mark offer-put offer-mark-on">p</span>{fmt.date(b.put_date)}
            {yearsTo(b.put_date) != null && <span className="mat-yrs mat-hz">{" (" + yearsTo(b.put_date) + ")"}</span>}
          </div>
        )}
      </td>
    ) },
  // ── рынок: стакан впереди последней сделки (торгуют по нему) ──
  { key: "g_spread_bid_bps", label: "BID", sub: "% / G-спред", align: "num", sep: true, w: 9,
    cell: (b) => <Quote key="bid" side="bid" px={b.bid} spread={b.g_spread_bid_bps}
      vwap={b._vwap_bid} title={qTitle(b, "bid")} /> },
  { key: "g_spread_ask_bps", label: "OFFER", sub: "% / G-спред", align: "num", w: 9,
    cell: (b) => <Quote key="ask" side="ask" px={b.ask} spread={b.g_spread_ask_bps}
      vwap={b._vwap_ask} title={qTitle(b, "ask")} /> },
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", grp: true, w: 7,
    cell: (b) => <td className={"num px-last" + (b.price_stale ? " px-stale" : "")} key="last_price_pct"
      title={b.price_stale ? "пред. закрытие MOEX — сделок сегодня не было" : undefined}>
      {fmt.pct(b.last_price_pct) ?? <D />}</td> },
  { key: "g_spread_wap_bps", label: "СР.ВЗВЕС", sub: "% / G-спред", align: "num", w: 11,
    cell: (b) => <Quote key="wap" side="wap" px={b.wap_pct} spread={b.g_spread_wap_bps}
      title="средневзвешенная цена дня и g-спред по ней — база аналитики (last price в неликвиде это один случайный принт)" /> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num", w: 8,
    cell: (b) => {
      const d = b.delta_to_prev_close;
      return <td className={"num " + (d == null ? "" : d >= 0 ? "pos" : "neg")} key="delta_to_prev_close">
        {d == null ? <D /> : <>{fmt.signed(d)} {d >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty", label: "DIRTY", sub: "RUB", align: "num", w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="dirty">{fmt.num(b.dirty) ?? <D />}</td> },
  // ── ликвидность ──
  { key: "val_today", label: "VOL", sub: "СЕГОДНЯ, М₽", align: "num", grp: true, w: 9,
    cell: (b) => <td className="num" key="val_today" title="оборот сегодня, ₽ (VALTODAY MOEX / тики Alor)">
      {fmt.mln(b.val_today) ?? <D />}</td> },
  { key: "adv_1m_rub", label: "ADV", sub: "1М, М₽", align: "num", w: 8,
    cell: (b) => <td className="num" key="adv_1m_rub"
      title="средний дневной оборот за 30 дней, ₽ — архив часовых баров / число торговых дней рынка">
      {fmt.mln(b.adv_1m_rub) ?? <D />}</td> },
  // ── доходности ──
  { key: "ytm", label: "YTM", sub: "К ПОГАШ. %", align: "num", grp: true, w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="ytm"
      title="эффективная доходность к погашению (к оферте, если поток обрезан на ней), % годовых">
      {fmt.pct(b.ytm) ?? <D />}</td> },
  { key: "delta_ytm", label: "Δ YTM", sub: "D/D пп", align: "num", w: 8,
    cell: (b) => <td className="num" style={b.delta_ytm == null ? undefined : dmColor(-b.delta_ytm)}
      key="delta_ytm">{b.delta_ytm == null ? <D /> : fmt.signed(b.delta_ytm)}</td> },
  { key: "cur_yield", label: "CUR Y", sub: "ТЕК. %", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} key="cur_yield">{fmt.pct(b.cur_yield) ?? <D />}</td> },
  // ── спреды ──
  { key: "g_spread_bps", label: "G-SPRD", sub: "vs КБД", align: "num", grp: true, w: 8,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.g_spread_bps)} key="g_spread_bps">
      {fmt.bps(b.g_spread_bps) ?? <D />}</td> },
  { key: "z_spread_bps", label: "Z-SPRD", sub: "vs КБД", align: "num", w: 8,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.z_spread_bps)} key="z_spread_bps">
      {fmt.bps(b.z_spread_bps) ?? <D />}</td> },
  // ── риск ──
  { key: "mod_dur", label: "DUR", sub: "МОД, лет", align: "num", grp: true, w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="mod_dur">{b.mod_dur == null ? <D /> : fmt.num(b.mod_dur, 2)}</td> },
  { key: "convexity", label: "CONV", sub: "ВЫПУКЛ.", align: "num", w: 8,
    cell: (b) => <td className={"num" + ms(b)} key="convexity">{b.convexity == null ? <D /> : fmt.num(b.convexity, 1)}</td> },
  { key: "dv01", label: "DV01", sub: "₽/БП", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} key="dv01">{b.dv01 == null ? <D /> : fmt.num(b.dv01, 2)}</td> },
];

export const FIXED_COL_META = FIXED_COLS.map(({ key, label, sub }) => ({ key, label, sub }));
export const FIXED_DEFAULT_COLS = FIXED_COLS.map((c) => c.key);
