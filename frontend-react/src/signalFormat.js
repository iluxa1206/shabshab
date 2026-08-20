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
  // Цвет по торговой конвенции, а не по «хорошо/плохо»: оффер (продают нам,
  // мы платим) — красный, бид (покупают у нас) — зелёный.
  return e.side === "ask"
    ? { text: "оффер", cls: "neg" }
    : { text: "бид", cls: "pos" };
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

/**
 * Причина повтора ДЕЛЬТОЙ: «спред +15 бп», «объём +30 %», «цена −0,6 п.п.».
 *
 * Само слово-ярлык («спред») не отвечает на главный вопрос — насколько ушло.
 * Пара «было → стало» отвечает, но её надо вычитать в уме; в ленте из двадцати
 * строк это не работает. Поэтому первым идёт знаковое приращение, а «было →
 * стало» остаётся в подсказке (см. reasonTitle).
 *
 * Единицы у каждой причины свои и совпадают с порогами бэка (services/signals):
 * спред — базисные пункты, цена — пункты цены, объём — проценты.
 */
const REASON_UNIT = {
  spread: { field: "val_bps", txt: "спред", fmt: (d) => `${fmt.devBps(d)} бп` },
  price: { field: "price", txt: "цена",
           fmt: (d) => `${d > 0 ? "+" : "−"}${fmt.num(Math.abs(d), 2)} п.п.` },
};

export function reasonDelta(e) {
  if (!e || e.reason === "block") return null;
  if (e.reason === "new") return "попала под условия";
  if (e.reason === "money") {
    // объём меряется деньгами В ГРАНИЦАХ спреда фильтра — процент от них
    const prev = e.prev_money_ok_rub, cur = e.money_ok_rub;
    if (prev == null || cur == null) return "объём появился";
    if (Math.abs(prev) < 1) return "объём появился";
    const pct = (cur - prev) / Math.abs(prev) * 100;
    return `объём ${pct > 0 ? "+" : "−"}${fmt.num(Math.abs(pct), 0)} %`;
  }
  const u = REASON_UNIT[e.reason];
  if (!u) return null;
  const prev = e["prev_" + u.field], cur = e[u.field];
  if (prev == null || cur == null || cur === prev) return u.txt;
  return `${u.txt} ${u.fmt(cur - prev)}`;
}

/** «было → стало» в подсказке: дельта отвечает «насколько», это — «от чего». */
export function reasonTitle(e) {
  if (!e || e.reason === "block" || e.reason === "new") return undefined;
  if (e.reason === "money") {
    return `объём по условиям фильтра: ${e.prev_money_ok_rub != null
      ? fmt.mln(e.prev_money_ok_rub) + " → " : ""}`
      + (e.money_ok_rub != null ? fmt.mln(e.money_ok_rub) + " млн ₽" : "—");
  }
  const u = REASON_UNIT[e.reason];
  if (!u) return undefined;
  const prev = e["prev_" + u.field], cur = e[u.field];
  const d = e.reason === "spread" ? 0 : 2;
  return `${u.txt}: ${prev != null ? fmt.num(prev, d) + " → " : ""}`
    + (cur != null ? fmt.num(cur, d) : "—");
}
