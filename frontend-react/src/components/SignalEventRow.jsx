import { fmt } from "../format.js";
import { bookMode, eventMoney, eventTag, maturityShort, maturityTxt, reasonDelta,
         reasonTitle, sideInfo, tradeMode, tradeTone } from "../signalFormat.js";

// единая единица проекта — млн ₽ голым числом (см. fmt.mln)
const money = (v) => (v == null ? "—" : fmt.mln(v));

// Подсказка происхождения события. Текст плашки живёт в signalFormat.eventTag.
const REASON_TITLE = {
  new: "бумага попала под условия",
  // цена больше НЕ повод для сигнала (спред уже несёт её движение), ярлык
  // оставлен для старых строк ленты
  price: "цена сдвинулась",
  spread: "спред ушёл на 5 бп",
  money: "объём по нашим условиям изменился",
  // не фильтр скринера, а рыночное событие: сделка крупнее порога уведомления
  // (в т.ч. адресная — РПС/размещение, которой в стакане не видно вообще)
  block: "крупная сделка по рынку",
};

const timeOf = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
};

/** Дельта к прошлому значению: показываем, НАСКОЛЬКО шевельнулось. */
export function Delta({ prev, cur, digits = 0, suffix = "" }) {
  if (prev == null || cur == null) return null;
  const d = cur - prev;
  if (!d) return null;
  const cls = d > 0 ? "pos" : "neg";
  return (
    <span className={"sb-delta " + cls}>
      {d > 0 ? "+" : "−"}{fmt.num(Math.abs(d), digits)}{suffix}
    </span>
  );
}

/**
 * Строка события сигнала — ОДНА на весь сайт: лента колокольчика и всплывающее
 * окно рисуют её одинаково. Раньше у тоста была своя вёрстка со своим набором
 * полей, и одно и то же событие в двух местах выглядело разными новостями.
 *
 * e — строка ленты с бэка либо match из WS-пуша (поля совпадают; время у
 * первой в fired_at, у второго в ts).
 */
/** Точка-разделитель между фактами строки — тише самих фактов. */
const Sep = () => <span className="sb-sep">·</span>;

/**
 * Строка события сигнала — ОДНА на весь сайт: лента колокольчика и всплывающее
 * окно рисуют её одинаково. Раньше у тоста была своя вёрстка со своим набором
 * полей, и одно и то же событие в двух местах выглядело разными новостями.
 *
 * Порядок фактов: сначала ЧТО за бумага и КУДА ушёл спред (ради этого строку
 * читают), потом когда/почём/сколько, и только третьей строкой — служебное
 * (чей фильтр, что за событие, ISIN).
 *
 * e — строка ленты с бэка либо match из WS-пуша (поля совпадают; время у
 * первой в fired_at, у второго в ts).
 */
export default function SignalEventRow({ e, onOpen, filterName }) {
  const title = REASON_TITLE[e.reason] || e.reason;
  // заливка фона — только у сделок: покупка зелёная, продажа красная, адресная
  // голубая. Читается боковым зрением, до того как глаз дошёл до плашки
  const tone = tradeTone(e);
  const isBlock = e.reason === "block";
  // срок пишем как в МОНИТОРЕ: дата и годы в скобках. Словесная форма («до
  // погашения 2,7 г») остаётся в подсказке.
  const mat = maturityTxt(e);
  const matShort = maturityShort(e);
  const mode = isBlock ? tradeMode(e) : bookMode(e);
  // дельта причины дублирует ± у спреда, когда шевельнулся именно спред; для
  // объёма, цены и первого попадания она несёт свою новость и остаётся нужной
  // (у «new» параметра в плашке нет — там стоит голая сторона)
  const why = !isBlock && e.reason !== "spread" ? reasonDelta(e) : null;
  return (
    <button type="button"
      className={"sb-row" + (tone ? " sb-t-" + tone : "")}
      onClick={() => onOpen?.(e)}
      title="Открыть карточку и стакан с подсветкой объёма">
      <span className="sb-row-1">
        {/* плашка первой и несёт сторону: у сделки это агрессор, у заявки —
            сторона очереди; параметр повтора стоит в её же скобках */}
        <span className={"sb-tag " + sideInfo(e).cls} title={title}>{eventTag(e)}</span>
        <span className="sb-name">{e.name || e.isin}</span>
        {matShort && <><Sep /><span className="sb-mat" title={mat}>{matShort}</span></>}
        {e.val_bps != null && (
          <>
            <Sep />
            {/* подпись «R-spread» ушла в подсказку: в первой строке важнее,
                чтобы число со знаком уместилось рядом с именем бумаги */}
            <b className="sb-val num" title="R-spread: IRR − доходность роллирования RUONIA">
              {fmt.num(e.val_bps, 0)} бп
              {/* единица уже названа рядом — дельта идёт голым числом */}
              <Delta prev={e.prev_val_bps} cur={e.val_bps} />
            </b>
          </>
        )}
        {/* объём — в первой же строке: «212 бп» без денег не говорит, стоит ли
            отрываться от дела; 900 млн и 5 млн — разные новости */}
        <Sep />
        <span className="sb-vol num">{money(eventMoney(e))} млн</span>
      </span>

      <span className="sb-row-2 num">
        <span className="sb-time">{timeOf(e.fired_at || e.ts)}</span>
        <Sep />
        <span className="sb-px">{fmt.num(e.price, 2)}%</span>
        <Delta prev={e.prev_price} cur={e.price} digits={2} />
        {mode && <><Sep /><span className="sb-mode">{mode}</span></>}
        {why && <><Sep />
          <span className="sb-why" title={reasonTitle(e)}>{why}</span></>}
      </span>

      <span className="sb-row-3">
        {/* у блока filter_name пустой, когда звонило умолчание (env-порог),
            а не заведённый пользователем фильтр */}
        {e.filter_name || filterName || (isBlock ? "крупная сделка" : "фильтр удалён")}
        <Sep />{e.isin}
      </span>
    </button>
  );
}
