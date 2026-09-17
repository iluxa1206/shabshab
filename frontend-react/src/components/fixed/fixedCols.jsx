import { fmt, dmColor, ratingColor, yearsTo, stripOfz } from "../../format.js";
import { D, IsinCopy, Quote, IssueCell, isFreshIssue, isShortBond } from "../BondTable.jsx";

// Колонки МОНИТОРА ФИКСОВ. Формат тот же, что у флоатеров (см. BondTable.COLS):
// key — ключ сортировки и видимости, w — ширина в ch по МАКСИМУМУ формата,
// sep/grp — вертикальные разделители блоков, cell(b) — полный <td>.
//
// Первичных метрик здесь ДВЕ равноправные: g-спред (к КБД ОФЗ) и доходность к
// погашению. Цветом по знаку красим спред; YTM всегда положительна, поэтому в
// своей колонке она без окраски, а под ценой котировки (BID/ASK/СР.ВЗВЕС) —
// зелёным акцентом, как спред у флоатеров: главная цифра ячейки одна на обе
// витрины и читается одинаково.
//
// ПОД ЦЕНОЙ в ячейках котировок (BID/ASK/СР.ВЗВЕС) стоит YTM по этой же цене, а
// не g-спред: у суверенной кривой спред сидит около нуля и читается как шум,
// тогда как доходность стороны — то, по чему торгуют. Сами g-спреды остались
// своими колонками (G-SPRD/Z-SPRD).
// WS тикнул цену, а производные (YTM/спреды/dirty) ещё от прошлого расчёта
// движка → dim-класс, чтобы трейдер не читал их как актуальные. То же правило
// и тот же класс, что у монитора флоатеров (BondTable).
const ms = (b) => (b._mstale ? " mstale" : "");

// Подпись котировки. Без фильтра по объёму — верх стакана MOEX; с ним — VWAP
// набора тикета по лестнице Alor (объём стороны в ₽ — b._vwap_bid/_vwap_ask).
function qTitle(b, side) {
  const base = side === "bid"
    ? "лучшая заявка на покупку (MOEX BID): чистая цена и доходность по ней (продажа в бид)"
    : "лучшая заявка на продажу (MOEX OFFER): чистая цена и доходность по ней (покупка с оффера)";
  const vol = side === "bid" ? b._vwap_bid : b._vwap_ask;
  if (!vol) return base;
  const lv = side === "bid" ? b._vwap_bid_levels : b._vwap_ask_levels;
  return `средневзвешенная цена набора ${fmt.num(vol / 1e6, 1)} млн ₽ (грязными) по стакану`
    + (lv ? `: ${lv} ур.` : "")
    + "; доходность посчитана к ней по методике (движок метрик, такт ≤5 с)";
}

export const FIXED_COLS = [
  // ── статика бумаги ──
  { key: "name", label: "INSTRUMENT", align: "left", w: 24,
    cell: (b) => (
      <td className="left name-cell" key="name">
        <div className={"bond-name" + (isFreshIssue(b.issue_date) ? " name-fresh" : "")}
             title={isFreshIssue(b.issue_date) ? "размещён " + fmt.date(b.issue_date) : undefined}>
          {/* бейдж только у ОФЗ (см. BondTable): «КОРП» — шум на каждой строке */}
          {b.cls === "ofz" && <span className="fx-cls fx-ofz">ОФЗ</span>}
          {b.face_unit && b.face_unit !== "RUB" && <span className="fx-cls" title="Валюта номинала выпуска">{b.face_unit}</span>}
          {(b.cls === "ofz" ? stripOfz(b.name) : b.name) || b.isin}
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
  { key: "maturity_date", label: "MATURITY", w: 17,
    // сортировка и окно срока меряют ту дату, к которой посчитаны метрики
    // строки (у фикса поток обрывается на оферте) — см. src/horizon.js
    title: "Срок до даты, к которой посчитаны метрики: оферта, если она есть "
           + "(дальше купоны неизвестны), иначе погашение. По нему же сортировка "
           + "колонки и окно срока в фильтрах",
    cell: (b) => (
      <td className="num mat-cell" key="maturity_date">
        <div className="mat-main">
          {fmt.date(b.maturity_date) ?? <D />}
          {yearsTo(b.maturity_date) != null && (
            <span className={"mat-yrs" + (b.put_date ? "" : " mat-hz")
                             + (!b.put_date && isShortBond(b) ? " mat-short" : "")}
                  title={!b.put_date && isShortBond(b) ? "короткая: до горизонта ≤ 6 мес" : undefined}>
              {" (" + yearsTo(b.maturity_date) + ")"}</span>
          )}
        </div>
        {b.put_date && (
          <div className="mat-offer" title={"оферта " + fmt.date(b.put_date) + " — метрики строки посчитаны к ней"}>
            <span className="offer-mark offer-put offer-mark-on">p</span>{fmt.date(b.put_date)}
            {yearsTo(b.put_date) != null && (
              <span className={"mat-yrs mat-hz" + (isShortBond(b) ? " mat-short" : "")}
                    title={isShortBond(b) ? "короткая: до горизонта ≤ 6 мес" : undefined}>
                {" (" + yearsTo(b.put_date) + ")"}</span>
            )}
          </div>
        )}
      </td>
    ) },
  { key: "issue_date", label: "РАЗМЕЩ", sub: "ДАТА (МЛРД)", w: 15,
    title: "Дата размещения (начало первого купона по графику MOEX), в скобках — объём выпуска "
           + "в млрд (размещено штук × номинал); свежие (≤30 дн) — зелёное имя",
    cell: (b) => <IssueCell b={b} key="issue_date" /> },
  { key: "issue_volume", label: "ОБЪЁМ", sub: "ВЫП., МЛРД", align: "num", w: 8,
    title: "Объём выпуска в валюте номинала, млрд: размещено штук (MOEX ISSUESIZEPLACED) × номинал; "
           + "у амортизируемых — по текущему номиналу, то есть объём в обращении",
    cell: (b) => <td className="num" key="issue_volume">{fmt.bln(b.issue_volume) ?? <D />}</td> },
  // ── рынок: стакан впереди последней сделки (торгуют по нему) ──
  { key: "ytm_bid", label: "BID", sub: "% / YTM", align: "num", sep: true, w: 9,
    cell: (b) => <Quote key="bid" side="bid" px={b.bid} spread={b.ytm_bid} subKind="pct"
      vwap={b._vwap_bid} title={qTitle(b, "bid")} /> },
  { key: "ytm_ask", label: "ASK", sub: "% / YTM", align: "num", w: 9,
    cell: (b) => <Quote key="ask" side="ask" px={b.ask} spread={b.ytm_ask} subKind="pct"
      vwap={b._vwap_ask} title={qTitle(b, "ask")} /> },
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", grp: true, w: 7,
    cell: (b) => <td className={"num px-last" + (b.price_stale ? " px-stale" : "")} key="last_price_pct"
      title={b.price_stale ? "пред. закрытие MOEX — сделок сегодня не было" : undefined}>
      {fmt.pct(b.last_price_pct) ?? <D />}</td> },
  { key: "ytm_wap", label: "СР.ВЗВЕС", sub: "% / YTM", align: "num", w: 11,
    cell: (b) => <Quote key="wap" side="wap" px={b.wap_pct} spread={b.ytm_wap} subKind="pct"
      title="средневзвешенная цена дня и доходность по ней — база аналитики (last price в неликвиде это один случайный принт)" /> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num", w: 8,
    cell: (b) => {
      const d = b.delta_to_prev_close;
      return <td className={"num " + (d == null ? "" : d >= 0 ? "pos" : "neg")} key="delta_to_prev_close">
        {d == null ? <D /> : <>{fmt.signed(d)} {d >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty", label: "DIRTY", sub: "НОМИНАЛ", align: "num", w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="dirty"
      title={`грязная цена одной бумаги в валюте номинала (${b.face_unit || "RUB"})`}>
      {fmt.num(b.dirty) ?? <D />} {b.dirty != null ? (b.face_unit || "RUB") : ""}</td> },
  // ── ликвидность ──
  { key: "val_today", label: "VOL", sub: "СЕГОДНЯ, М₽", align: "num", grp: true, w: 9,
    cell: (b) => <td className="num" key="val_today" title="оборот сегодня, ₽ (VALTODAY MOEX / тики Alor)">
      {fmt.mln(b.val_today) ?? <D />}</td> },
  { key: "adv_1m_rub", label: "ADV", sub: "1М, М₽", align: "num", w: 8,
    cell: (b) => <td className="num" key="adv_1m_rub"
      title="средний дневной оборот за 30 дней, ₽ — архив часовых баров / число торговых дней рынка">
      {fmt.mln1(b.adv_1m_rub) ?? <D />}</td> },
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
  { key: "dv01", label: "DV01", sub: "НОМ/БП", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} key="dv01"
      title={`изменение цены одной бумаги на 1 б.п. в валюте номинала (${b.face_unit || "RUB"})`}>
      {b.dv01 == null ? <D /> : fmt.num(b.dv01, 2)}</td> },
];

export const FIXED_COL_META = FIXED_COLS.map(({ key, label, sub }) => ({ key, label, sub }));
export const FIXED_DEFAULT_COLS = FIXED_COLS.map((c) => c.key);

// Колонки, которых нет в мониторе ФИКСОВ, но которые дописывает витрина ОФЗ
// (/fixed/ofz) — та же таблица, тот же формат. Держим ОТДЕЛЬНО от FIXED_COLS:
// ΔYTM к дате сравнения считается только там, где есть сама дата (сдвиг
// кривой «вчера/дата» у ОФЗ), в мониторе фиксов это была бы колонка прочерков.
//
// Значение — b.d_ytm_cmp в б.п. ((ytm_now − ytm_asof) × 100), его кладёт в
// строку OfzDesk из ответа /api/fixed/ofz/asof. Знак нужен: колонка про
// «выше/ниже, чем было», без сравнения — прочерк.
export const OFZ_EXTRA_COLS = [
  // отклонение от СВОЕЙ кривой ОФЗ (NSS по точкам выпусков, /ofz/curve) —
  // главное число витрины; G-SPRD в общих колонках остаётся к КБД (движок)
  { key: "resid_bps", label: "Δ КРИВАЯ", sub: "К СВОЕЙ, БП", align: "num", grp: true, w: 9,
    title: "отклонение YTM от своей кривой ОФЗ (NSS по точкам выпусков, по выбранной базе цены), б.п.: плюс — выше кривой, выпуск дешевле соседей",
    cell: (b) => {
      const d = b.resid_bps;
      return <td className={"num" + (d == null || d === 0 ? "" : d > 0 ? " pos" : " neg")} key="resid_bps"
        title={b.curve_used === false ? "вне подгонки кривой (короче 0,25 г или YTM вне диапазона)" : undefined}>
        {d == null ? <D /> : fmt.signed(d, 0)}</td>;
    } },
  { key: "d_ytm_cmp", label: "ΔYTM", sub: "К ДАТЕ, БП", align: "num", grp: true, w: 9,
    title: "сдвиг доходности к дате сравнения, б.п.: плюс — доходность выросла (бумага подешевела)",
    cell: (b) => {
      const d = b.d_ytm_cmp;
      return <td className={"num" + (d == null || d === 0 ? "" : d > 0 ? " pos" : " neg")} key="d_ytm_cmp">
        {d == null ? <D /> : fmt.signed(d, 0)}</td>;
    } },
];
