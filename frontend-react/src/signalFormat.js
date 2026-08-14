/**
 * Подписи события ленты сигналов — общие для вкладки СИГНАЛЫ и колокольчика.
 *
 * Строка события смешивает две разные новости: срабатывание фильтра стакана
 * (сторона = оффер/бид, объём = набор по лестнице) и крупную сделку (сторона =
 * агрессор, объёма стакана нет вовсе). Голые числа «−26 бп · 100,04%» без
 * подписи читаются одинаково, поэтому режим, сторону, базу спреда и срок до
 * погашения называем явно.
 */
import { fmt } from "./format.js";

/** Сторона: у сделки — агрессор, у стакана — сторона очереди. */
export function sideInfo(e) {
  if (e.reason === "block") {
    if (e.side === "buy") return { text: "покупка", cls: "pos" };
    if (e.side === "sell") return { text: "продажа", cls: "neg" };
    // РПС/размещение: агрессора нет, обе стороны договорились заранее
    return { text: "без агрессора", cls: "dim" };
  }
  return e.side === "ask"
    ? { text: "оффер", cls: "pos" }
    : { text: "бид", cls: "neg" };
}

/** Режим торгов сделки: адресная (РПС) или обычная по стакану. */
export function tradeMode(e) {
  if (e.reason !== "block") return null;
  // negotiated пишется с 2026-08-14; у старых строк ленты его нет
  if (e.negotiated == null) return null;
  return e.negotiated ? "адресная (РПС)" : "по стакану";
}

/** Как набран объём у события стакана: лестницей или одной заявкой. */
export function bookMode(e) {
  if (e.reason === "block") return null;
  if (e.money_mode === "single" || (e.money_mode == null && e.single_px != null)) {
    return e.single_px != null
      ? `одна заявка ${fmt.num(e.single_px, 2)}%` : "одна заявка";
  }
  return e.levels ? `набор ${e.levels} ур` : "набор по стакану";
}

/** «до погашения 2,7 г» — срок считается на сервере на момент чтения ленты. */
export function maturityTxt(e) {
  if (e.years == null && !e.maturity) return null;
  const d = e.maturity ? fmt.date(e.maturity) : null;
  if (e.years == null) return `погашение ${d}`;
  const y = e.years < 1
    ? `${Math.round(e.years * 12)} мес`
    : `${fmt.num(e.years, 1)} г`;
  return d ? `погашение ${d} · ${y}` : `до погашения ${y}`;
}
