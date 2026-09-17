import { cloneElement, memo, useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";
import { baseLabel, fmt, dmColor, ratingColor, yearsTo, yearsToNum, stripOfz } from "../format.js";
import IsinCopyBase from "./IsinCopy.jsx";
import CouponFormula from "./CouponFormula.jsx";
import { HeaderCell } from "./TableHeader.jsx";
import { horizonDate } from "../horizon.js";

export const D = () => <span className="dash">—</span>;

// ISIN под именем выпуска: общий копирующий компонент (components/IsinCopy),
// здесь только стиль места — своя строка под именем.
export function IsinCopy({ isin }) {
  return <IsinCopyBase isin={isin} className="isin-copy-blk" />;
}

// WS тикнул цену, но производные метрики (DM/SM/z/dirty/CHG/Y-IDX) ещё
// от прошлого расчёта бэка → dim-класс, чтобы трейдер не читал их как актуальные.
const ms = (b) => (b._mstale ? " mstale" : "");

function Chip({ value }) {
  if (value == null) return <D />;
  return <span className="dm-chip" style={dmColor(value)}>{fmt.bps(value)} {value >= 0 ? "▲" : "▼"}</span>;
}

// spread по цене СРЕДНЕВЗВЕСА — число бэкенда, посчитанное по методике
// (движок метрик считает средневзвес такой же альт-ценой, как bid/ask).
// Раньше здесь стояла линеаризация от якоря «бэк для этой цены не считает» —
// с 27.08.2026 считает, а линия через якорь уводила число вслед за якорем.
// Нет числа — прочерк: догадка на месте спреда хуже пустой ячейки.
export function wapSpread(b) {
  return b.y_idx_wap_bps ?? null;
}

// Котировка двумя этажами в одной ячейке: чистая цена, под ней spread по ней
// же. Две колонки вместо четырёх — глаз читает пару «цена/спред» как одно
// значение, а не бегает через полтаблицы, чтобы их сопоставить.
//
// base7 — база недели: у СРЕДНЕВЗВЕСА рядом со спредом мелким серым идёт
// отклонение от неё («120 +20»). У котировок стакана база не рисуется: там и так
// две цифры, а сравнивать с историей осмысленно цену сделок, а не заявку.
// subKind — что писать ПОД ценой: "bps" (спред, по умолчанию) или "pct"
// (доходность). У фиксов вторая строка ячейки — YTM по этой же цене: знака у
// неё нет, поэтому ни цвета, ни отклонения от семидневки там не рисуем.
export function Quote({ px, spread, stale, title, vwap, side, base7, subKind = "bps" }) {
  // фон стороны: бид зелёным, оффер красным, почти прозрачно, чтобы не спорить
  // с цветом спреда под ценой. Средневзвес — ничья цена, фон нейтральный
  const cls = side === "bid" ? " q-bid" : side === "ask" ? " q-ask" : "";
  // Ноль = стороны в стакане нет (источники отдают 0 вместо null). Гасим и
  // цену, и спред: «0,00» с четырёхзначным спредом под ним читались как
  // настоящая котировка. Бэк нормализует то же самое, здесь — страховка на
  // случай строк из старого кэша.
  if (!(px > 0)) { px = null; spread = null; stale = null; }
  // ЦЕНА УШЛА, СПРЕД ЕЩЁ СЧИТАЕТСЯ — показываем последнее число приглушённым.
  // Пустая клетка говорила «спреда нет», хотя порядок величины известен и на
  // порядок не изменится; выдавать его за посчитанный тоже нельзя — отсюда
  // полупрозрачность и подпись. Число к ПРЕЖНЕЙ цене, поэтому отклонение от
  // семидневки рядом с ним не рисуем: сравнивать было бы не с чем.
  const old = spread == null && stale != null;
  const shown = spread ?? (old ? stale : null);
  // заявки нет вовсе — один прочерк, а не два друг под другом
  if (px == null && shown == null) return <td className={"num" + cls} title={title}><D /></td>;
  const pctSub = subKind === "pct";
  const dev = pctSub || spread == null || base7 == null ? null : spread - base7;
  return (
    <td className={"num q-cell" + cls} title={title}>
      <div className={"q-px" + (vwap ? " q-vwap" : "")}>{fmt.pct(px) ?? <D />}</div>
      {/* под ценой: спред (знак → цвет) у флоатеров, YTM у фиксов — тем же
          акцентом, чтобы главная цифра ячейки читалась одинаково в обеих витринах */}
      <div className={"q-sp" + (old ? " q-sp-old" : "")}
           style={shown == null ? undefined : dmColor(shown)}
           title={old ? (pctSub ? "доходность к прежней цене — пересчитывается"
                                : "спред к прежней цене — пересчитывается") : undefined}>
        {shown == null ? <D /> : pctSub ? fmt.pct(shown) : fmt.bps(shown)}
        {dev != null && (
          <span className="q-sp-dev"
            title={`отклонение от средневзвешенного спреда за 7 дней (${fmt.bps(base7)} бп)`}>
            {fmt.devBps(dev)}</span>
        )}
      </div>
    </td>
  );
}

// Ячейка РАЗМЕЩ: дата и в скобках объём выпуска в млрд — одна на мониторы
// флоатеров и фиксов (fixedCols её импортирует).
export function IssueCell({ b }) {
  const vol = fmt.bln(b.issue_volume);
  return (
    <td className={"num" + (isFreshIssue(b.issue_date) ? " issue-fresh" : "")}>
      {fmt.date(b.issue_date) ?? <D />}
      {vol != null && <span className="issue-vol" title={"объём выпуска, млрд " + (b.face_unit || "₽")}>{" (" + vol + ")"}</span>}
    </td>
  );
}

// Подпись ячейки котировки. Без фильтра по объёму — верх стакана MOEX; с ним —
// VWAP набора тикета по лестнице Alor (объём стороны в ₽ — b._vwap_bid/_vwap_ask).
function qTitle(b, side) {
  const base = side === "bid"
    ? "лучшая заявка на покупку (MOEX BID): чистая цена и spread по ней (продажа в бид)"
    : "лучшая заявка на продажу (MOEX OFFER): чистая цена и spread по ней (покупка с оффера)";
  const vol = side === "bid" ? b._vwap_bid : b._vwap_ask;
  if (!vol) return base;
  const lv = side === "bid" ? b._vwap_bid_levels : b._vwap_ask_levels;
  const mln = fmt.num(vol / 1e6, 1);
  return `средневзвешенная цена набора ${mln} млн ₽ (грязными) по стакану`
    + (lv ? `: ${lv} ур.` : "")
    + `; spread посчитан к ней по методике (движок метрик, такт ≤5 с)`;
}

// Маркеры оферты перед датой погашения. p и c — РАЗНЫЕ факты из разных источников,
// не взаимоисключающие: p — ближайшая будущая оферта из MOEX bondization (дата
// известна, рынок прайсит бумагу к ней); c — call-опцион эмитента из corpbonds
// (даты нет: MOEX в offertype колл не различает вовсе). У бумаги может быть и то,
// и другое → рисуем «pc». has_call === false («колла нет») и null («не знаем»)
// одинаково молчат: маркер утверждает наличие, а не отсутствие.
export function OfferMarks({ b }) {
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
// Свежий выпуск: размещён не старше 30 календарных дней — имя зелёное, как
// «новое» в первичке. Порог тот же, что у очереди свежих выпусков в Справочнике.
const NEW_ISSUE_DAYS = 30;
export const isFreshIssue = (iso) => {
  if (!iso) return false;
  const t = Date.parse(String(iso).slice(0, 10) + "T00:00:00Z");
  return Number.isFinite(t) && Date.now() - t <= NEW_ISSUE_DAYS * 864e5;
};
// «Прям сильно короткая»: до горизонта прайсинга ≤ полугода — годы красные
const SHORT_YRS = 0.5;
export const isShortBond = (b) => {
  const y = yearsToNum(horizonDate(b));
  return y != null && y >= 0 && y <= SHORT_YRS;
};

export const COLS = [
  // ── статика бумаги ──
  { key: "short_name", label: "INSTRUMENT", align: "left", w: 24,
    cell: (b) => {
      // ОФЗ-ПК (суверенные флоатеры) — имя MOEX «ОФЗ 29xxx»; остальное — корпораты
      const isOfz = /^\s*ОФЗ/i.test(b.short_name || "");
      const fresh = isFreshIssue(b.issue_date);
      return (
        <td className="left name-cell" key="short_name">
          <div className={"bond-name" + (fresh ? " name-fresh" : "")}
               title={fresh ? "размещён " + fmt.date(b.issue_date) : undefined}>
            {/* бейдж только у ОФЗ: «КОРП» стоял в 9 строках из 10 и ничего не
                различал. Из имени слово ОФЗ срезано — его несёт бейдж */}
            {isOfz && <span className="fx-cls fx-ofz">ОФЗ</span>}
            {(isOfz ? stripOfz(b.short_name) : b.short_name) || b.isin}
            {/* рейтинг здесь же, цветом бакета (как в фильтрах) — отдельной колонки не держим */}
            {b.rating && <span className="bond-rt" style={{ color: ratingColor(b.rating) }}>({b.rating})</span>}
            {b.price_implausible && <span className="badge-stale" title="Цена подразумевает номинальный убыток (dirty > Σ будущих потоков) — вероятно стейл/тонкая цена неликвида. Спреды скрыты.">стейл</span>}
            {!b.price_implausible && b.price_thin && <span className="badge-thin" title="0 сделок сегодня на MOEX — цена несвежая (вчерашний/старый принт). DM/z сняты с ненадёжной цены.">тонк</span>}
          </div>
          <div className="isin-row">
            <IsinCopy isin={b.isin} />
            {/* Эксперт РА / АКРА через слэш; прочерк — агентство бумагу не
                оценивало. Рядом с именем стоит ХУДШАЯ из них — по ней фильтр. */}
            {b.ratings_ea && (
              <span className="isin-rt" title="Эксперт РА / АКРА">({b.ratings_ea})</span>
            )}
          </div>
        </td>
      );
    } },
  // Линкер помечен прямо в бейдже базы («RU·И»): у такой бумаги по базе растёт
  // НОМИНАЛ, а ставка купона фиксирована — без метки строка «RU + 1,85%»
  // читалась бы как флоатер с абсурдно узкой маржой.
  { key: "base_rate_type", label: "BASE", w: 6,
    cell: (b) => <td key="base_rate_type"><span className={"badge " + b.base_rate_type}
      title={b.face_index ? b.face_index + ": индексируемый номинал, ставка купона фиксирована"
                          : b.base_rate_type}>
      {baseLabel(b.base_rate_type) + (b.face_index ? "·И" : "")}</span></td> },
  { key: "formula", label: "FORMULA", align: "left", w: 15,
    cell: (b) => <td className="left bond-formula" key="formula">
      <CouponFormula base={b.base_rate_type} spreadBps={b.spread_issue_bps}
        faceIndex={b.face_index}
        couponsPerYear={b.coupons_per_year} formula={b.formula} /></td> },
  // У линкера в этой колонке НЕ спред к базе, а фиксированная ставка купона:
  // складывать её с RUONIA нельзя, по индексу растёт номинал. Значение то же,
  // подпись в подсказке.
  { key: "spread_issue_bps", label: "МАРЖА", sub: "ВЫПУСК, БП", align: "num", w: 8,
    cell: (b) => <td className="num" key="spread_issue_bps"
      title={b.face_index ? "фиксированная ставка купона, а не спред к базе: по базе растёт номинал" : undefined}>
      {b.spread_issue_bps != null ? (b.face_index ? "" : "+") + b.spread_issue_bps : <D />}</td> },
  { key: "next_coupon_date", label: "COUPON", sub: "NEXT", w: 10,
    cell: (b) => <td className="num" style={{ fontSize: 12 }} key="next_coupon_date">{fmt.date(b.next_coupon_date) ?? <D />}</td> },
  // Два этажа: погашение с годами до него, под ним — дата оферты (если есть) с
  // годами до неё, мелким и серым. Дата ГОРИЗОНТА ПРАЙСИНГА (той, к которой
  // посчитан спред строки) помечена СИНИМИ ГОДАМИ в скобках — но только когда
  // выбор реально был: без оферты и колла горизонт один, и подсветка каждой
  // строки ничего не сообщает.
  // w=17: «p 10.10.2029 (4.2)» — ширина по МАКСИМУМУ формата, иначе появление
  // маркера или второго этажа у одной бумаги дёргает колонку.
  { key: "maturity_date", label: "MATURITY", w: 17,
    // сортировка и окно срока меряют ГОРИЗОНТ ПРАЙСИНГА (см. src/horizon.js) —
    // ту дату, что подсвечена синим; шапка обязана про это сказать, иначе
    // порядок строк выглядит сломанным
    title: "Срок до горизонта прайсинга — синие годы: оферта, если рынок прайсит "
           + "к ней (правило цены), иначе погашение. По нему же сортировка колонки "
           + "и окно срока в фильтрах",
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
              <span className={"mat-yrs" + (hasChoice && hz === "maturity" ? " mat-hz" : "")
                               + (isShortBond(b) && (!hasChoice || hz === "maturity") ? " mat-short" : "")}
                    title={isShortBond(b) && (!hasChoice || hz === "maturity") ? "короткая: до горизонта ≤ 6 мес" : undefined}>
                {" (" + yearsTo(b.maturity_date) + ")"}</span>
            )}
          </div>
          {b.offer_date && (
            <div className="mat-offer"
              title={(b.offer_kind === "call" ? "call-оферта " : "пут-оферта ") + fmt.date(b.offer_date)}>
              <OfferMarks b={b} />{fmt.date(b.offer_date)}
              {yearsTo(b.offer_date) != null && (
                <span className={"mat-yrs" + (hz === "put" || hz === "call" ? " mat-hz" : "")
                                 + (isShortBond(b) && (hz === "put" || hz === "call") ? " mat-short" : "")}
                      title={isShortBond(b) && (hz === "put" || hz === "call") ? "короткая: до горизонта ≤ 6 мес" : undefined}>
                  {" (" + yearsTo(b.offer_date) + ")"}</span>
              )}
            </div>
          )}
        </td>
      );
    } },
  { key: "issue_date", label: "РАЗМЕЩ", sub: "ДАТА (МЛРД)", w: 15,
    title: "Дата размещения по реестру, в скобках — объём выпуска в млрд (размещено штук × номинал, "
           + "MOEX); свежие (≤30 дн) — зелёное имя",
    cell: (b) => <IssueCell b={b} key="issue_date" /> },
  { key: "issue_volume", label: "ОБЪЁМ", sub: "ВЫП., МЛРД", align: "num", w: 8,
    title: "Объём выпуска в валюте номинала, млрд: размещено штук (MOEX ISSUESIZEPLACED) × номинал; "
           + "у амортизируемых — по текущему номиналу, то есть объём в обращении",
    cell: (b) => <td className="num" key="issue_volume">{fmt.bln(b.issue_volume) ?? <D />}</td> },
  // ── НАША МОДЕЛЬ (стакан → последняя сделка → dirty → spread (первичная) → SM → DM → Z) ──
  // Верх стакана MOEX (board snapshot, TTL 120с — не WS-тик): цена и Y-IDX по ней
  // в ОДНОЙ ячейке (цена сверху, спред под ней) — две колонки вместо четырёх.
  // Сортировка колонки — по Y-IDX: цены разных бумаг между собой несравнимы,
  // спред — да. Стакан идёт ПЕРВЫМ: торгуют по нему, а last — уже история.
  { key: "y_idx_bid_bps", label: "BID", align: "num", sep: true, w: 8,
    cell: (b) => <Quote key="bid" side="bid" px={b.bid_price_pct} spread={b.y_idx_bid_bps}
      stale={b.y_idx_bid_stale} vwap={b._vwap_bid} title={qTitle(b, "bid")} /> },
  { key: "y_idx_ask_bps", label: "ASK", align: "num", w: 8,
    cell: (b) => <Quote key="ask" side="ask" px={b.ask_price_pct} spread={b.y_idx_ask_bps}
      stale={b.y_idx_ask_stale} vwap={b._vwap_ask} title={qTitle(b, "ask")} /> },
  // последняя сделка и всё, что от неё производно (движение, dirty) — своя группа
  { key: "last_price_pct", label: "PRICE", sub: "CLN %", align: "num", grp: true, w: 7,
    cell: (b) => <td className={"num px-last" + (b.price_stale ? " px-stale" : "")} key="last_price_pct"
      title={b.price_stale ? "пред. закрытие MOEX — нет сделок сегодня / не в Alor-потоке" : undefined}>
      {fmt.pct(b.last_price_pct) ?? <D />}</td> },
  // Средневзвес дня. У избранного — НАШ VWAP по тикам Alor (живой, тот же, что
  // рисует слой «Средневзвес» на графике), у остальных — биржевой WAPRICE из
  // снапшота MOEX. Отсюда и подпись в title: источники разные.
  // Спред под ценой — по той же схеме, что у BID/OFFER: spread по цене
  // средневзвеса и мелким серым его отклонение от базы недели.
  { key: "wap_price_pct", label: "СР.ВЗВЕС", sub: "% / spread", align: "num", w: 11,
    cell: (b) => <Quote key="wap_price_pct" side="wap" px={b.wap_price_pct} spread={wapSpread(b)}
      stale={b.y_idx_wap_stale}
      base7={b.y_idx_avg7_bps}
      title={(b._live ? "наш VWAP по сделкам дня (live)" : "WAPRICE MOEX, средневзвес дня")
        + "; spread посчитан к этой цене по методике (движок метрик)"} /> },
  { key: "delta_to_prev_close", label: "CHG", sub: "PREV", align: "num", w: 8,
    cell: (b) => {
      const delta = b.delta_to_prev_close;
      const deltaCls = delta == null ? "" : delta >= 0 ? "pos" : "neg";
      return <td className={"num " + deltaCls} key="delta_to_prev_close">{delta == null ? <D /> : <>{fmt.signed(delta)} {delta >= 0 ? "▲" : "▼"}</>}</td>;
    } },
  { key: "dirty_price_rub", label: "DIRTY", sub: "RUB", align: "num", w: 9,
    cell: (b) => <td className={"num" + ms(b)} key="dirty_price_rub">{fmt.num(b.dirty_price_rub) ?? <D />}</td> },
  // ── ликвидность: оборот сегодня и средний дневной за месяц ──
  // Обе колонки в млн ₽. VOL — Σ сделок дня по тикам Alor (живой, растёт
  // сделка в сделку), с откатом на биржевой VALTODAY снапшота, если своего
  // счёта нет; ADV — Σ денег архива часовых баров за 30 дней / число торговых
  // дней рынка. Разные источники, поэтому подписи разведены.
  { key: "val_today", label: "VOL", sub: "СЕГОДНЯ, М₽", align: "num", grp: true, w: 9,
    cell: (b) => <td className="num" key="val_today"
      title={b._live ? "оборот сегодня, ₽ — Σ сделок по тикам (live)"
        : "оборот сегодня, ₽ (тики Alor / VALTODAY MOEX)"}>{fmt.mln(b.val_today) ?? <D />}</td> },
  { key: "adv_1m_rub", label: "ADV", sub: "1М, М₽", align: "num", w: 8,
    cell: (b) => <td className="num" key="adv_1m_rub"
      title={"средний дневной оборот за 30 дней, ₽ — Σ денег архива часовых баров / "
        + "число торговых дней рынка (не дней, когда торговалась эта бумага)"}>
      {fmt.mln1(b.adv_1m_rub) ?? <D />}</td> },
  { key: "yield_over_index_bps", label: "SPREAD", sub: "IRR−ИНДЕКС", align: "num", grp: true, w: 11,
    cell: (b) => <td className={"num" + ms(b) + (b._yoi_stale ? " q-sp-old" : "")}
      key="yield_over_index_bps"
      title={b._yoi_stale ? "спред к прежней цене сделки — пересчитывается" : undefined}>
      <Chip value={b.yield_over_index_bps} /></td> },
  // Маржи в ВИТРИНЕ выключены (services/universe.MARGINS_IN_UNIVERSE): каждая
  // это солвер, а DM вдобавок пересобирает поток — 78–92 % расчёта бумаги ради
  // колонки, по которой не торгуют. Первичная метрика здесь Y-IDX; маржи живут
  // в карточке, калькуляторе и ленте. Вернуть в витрину: VALUATION_MARGINS=1.
  { key: "dm_bps", label: "SM", sub: "MODEL", align: "num", grp: true, w: 7,
    title: "Simple margin. В витрине выключен ради скорости — число есть в карточке бумаги",
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.dm_bps)} key="sm_bps">{fmt.bps(b.dm_bps) ?? <D />}</td> },
  { key: "disc_margin_bps", label: "DM", sub: "MODEL", align: "num", w: 7,
    title: "Discount margin. В витрине выключен ради скорости — число есть в карточке бумаги",
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.disc_margin_bps)} key="disc_margin_bps">{fmt.bps(b.disc_margin_bps) ?? <D />}</td> },
  { key: "z_model_bps", label: "OUR Z", sub: "vs КБД", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} style={dmColor(b.z_model_bps)} key="z_model_bps">{fmt.bps(b.z_model_bps) ?? <D />}</td> },
  { key: "yield_xirr_pct", label: "YTM", sub: "БОНД %", align: "num", w: 7,
    cell: (b) => <td className={"num" + ms(b)} key="yield_xirr_pct">{b.yield_xirr_pct == null ? <D /> : fmt.pct(b.yield_xirr_pct)}</td> },
  { key: "index_yield_pct", label: "YTM", sub: "RUONIA %", align: "num", w: 7,
    cell: (b) => <td className="num" key="index_yield_pct" title="доходность роллирования RUONIA до погашения — база spread (общая для КС и RUONIA бумаг)">{b.index_yield_pct == null ? <D /> : fmt.pct(b.index_yield_pct)}</td> },
];

// метаданные для меню видимости (без cell-функций)
export const COL_META = COLS.map(({ key, label, sub }) => ({ key, label, sub }));
export const DEFAULT_COLS = COLS.map((c) => c.key);

// memo: WS-тик цены пересобирает массив rows, но ссылки НЕИЗМЕНИВШИХСЯ бумаг
// стабильны (App точечно клонирует только тикнувшую) — memo снимает ре-рендер
// остальных ~450 строк. Требует стабильных onOpen/onToggleStar (useCallback в App)
// и стабильного cols (useMemo ниже). Flash — CSS-анимация tr.flash (styles.css)
// вместо framer-инстанса на строку.
const BondRow = memo(function BondRow({ b, onOpen, starred, onToggleStar, cols, kind }) {
  const prev = useRef(b.last_price_pct);
  const reduce = useReducedMotion();
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (prev.current != null && b.last_price_pct !== prev.current && !reduce) {
      setFlash(true);
    }
    prev.current = b.last_price_pct;
  }, [b.last_price_pct, reduce]);

  const open = (e) => onOpen(b.isin, e.currentTarget, kind);
  const toggleStar = (e) => { e.stopPropagation(); onToggleStar(b.isin); };

  return (
    <tr
      className={flash ? "flash" : ""}
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

// colsDef/defaultCols — витрина, для которой рисуем таблицу. По умолчанию
// флоатерная (COLS): у МОНИТОРА фиксов свой набор колонок того же формата
// (components/fixed/fixedCols.jsx), всё остальное поведение общее.
export default function BondTable({ rows, status, errMsg, sort, onSort, onOpen, watch = [], onToggleStar, filtered, onClearFilters, onRetry, visibleCols, onMoveCol, colWidths = {}, onResizeCol, onResetColWidth, colsDef = COLS, defaultCols = DEFAULT_COLS, rowKind, colProgress }) {
  // ПОРЯДОК КОЛОНОК = порядок visibleCols (его задаёт пользователь перетаскиванием),
  // а не порядок объявления colsDef. useMemo — стабильная ссылка для memo(BondRow).
  const cols = useMemo(() => {
    const byKey = new Map(colsDef.map((c) => [c.key, c]));
    const keys = visibleCols?.length ? visibleCols : defaultCols;
    const out = keys.map((k) => byKey.get(k)).filter(Boolean);
    return out.length ? out : colsDef;   // пустой/битый набор → дефолт, а не голая таблица
  }, [visibleCols, colsDef, defaultCols]);
  // какую колонку тащим и над какой висим — для подсветки цели (ref — источник
  // правды в обработчиках, state только для стилей)
  const dragRef = useRef(null);
  const [dragKey, setDragKey] = useState(null);
  const [overKey, setOverKey] = useState(null);
  // O(1) вместо watch.includes на каждую строку
  const watchSet = useMemo(() => new Set(watch), [watch]);
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
    <BondRow key={b.isin} b={b} onOpen={onOpen} starred={watchSet.has(b.isin)} onToggleStar={onToggleStar} cols={cols} kind={rowKind} />
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
              overKey={overKey} setOverKey={setOverKey} progress={colProgress?.[c.key]}
              onResizeCol={onResizeCol} onResetColWidth={onResetColWidth} />)}
            <th className="fill-col" aria-hidden="true" />
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </section>
  );
}
