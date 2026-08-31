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

/**
 * ПЛАШКА события: сторона + что именно случилось. Одна рамка отвечает сразу на
 * два вопроса, ради которых строку читают, и красится в цвет стороны.
 *
 * Заявка (событие стакана) — сторона очереди: «БИД» / «ОФФЕР», а при повторе в
 * скобках стоит ПАРАМЕТР, который сдвинулся: «ОФФЕР (СПРЕД)». Раньше плашка
 * говорила «заявка», а сторона жила отдельным словом рядом — два места про
 * одно событие.
 *
 * Сделка (крупный принт) — сторона АГРЕССОРА словом: «ПОКУПКА» / «ПРОДАЖА».
 * У адресной (РПС) агрессора нет — остаётся голое «СДЕЛКА».
 *
 * Регистр делает CSS (.sb-tag, text-transform: uppercase).
 */
export function eventTag(e) {
  if (!e) return "";
  if (e.reason === "block") {
    // адресная идёт без стороны ДАЖЕ если поле side есть: у РПС стороны
    // договорились заранее, агрессора нет, и слово утверждало бы неправду
    if (e.negotiated) return "сделка";
    if (e.side === "buy") return "покупка";
    if (e.side === "sell") return "продажа";
    return "сделка";
  }
  const side = e.side === "ask" ? "оффер" : "бид";
  const param = { spread: "спред", money: "объём", price: "цена" }[e.reason];
  return param ? `${side} (${param})` : side;
}

/**
 * Тон строки сделки для фоновой заливки: покупка / продажа / адресная.
 * null у события стакана — заливка красит ТОЛЬКО сделки, иначе цвет перестаёт
 * что-либо значить.
 *
 * Адресную берём по negotiated, а не по «нет стороны»: у старых строк ленты
 * (до 2026-08-14) поля нет вовсе, и они попадают в rps фолбэком — это верно
 * по смыслу, потому что сторону мы не знаем ровно у адресных.
 */
export function tradeTone(e) {
  if (!e || e.reason !== "block") return null;
  if (e.negotiated) return "rps";
  if (e.side === "buy") return "buy";
  if (e.side === "sell") return "sell";
  return "rps";
}

/** Режим торгов сделки: адресная (РПС) или обычная по стакану. */
export function tradeMode(e) {
  if (e.reason !== "block") return null;
  // negotiated пишется с 2026-08-14; у старых строк ленты его нет
  if (e.negotiated == null) return null;
  return e.negotiated ? "адресная (РПС)" : "по стакану";
}

/**
 * Место заявки в очереди: «best» — стоит первой по цене на своей стороне.
 *
 * Раньше здесь стоял счёт уровней набора («набор 3 ур») — механика фильтра, а
 * не новость. Стол спрашивает другое: первая заявка или за чужими спинами. Не
 * первая — не пишем ничего: строка короче, а молчание само по себе ответ.
 *
 * Режим «одна крупная заявка» остаётся отдельной подписью с ценой: там важно,
 * что объём стоит одним тикетом, а не собран лестницей.
 */
export function bookMode(e) {
  if (e.reason === "block") return null;
  if (e.money_mode === "single" || (e.money_mode == null && e.single_px != null)) {
    const one = e.single_px != null
      ? `одна заявка ${fmt.num(e.single_px, 2)}%` : "одна заявка";
    return e.best ? `${one} · best` : one;
  }
  return e.best ? "best" : null;
}

/**
 * Срок в скобках — «(1,3 г)». Берём ГОТОВОЕ e.years с бэка: там оно считается
 * до ГОРИЗОНТА ПРАЙСИНГА (оферта/колл по правилу цены, иначе погашение) той же
 * функцией, что фильтрует окно срока в МОНИТОРЕ (screener_core.horizon_years),
 * и пересчитывается на каждом чтении ленты. Считать годы здесь от e.maturity
 * нельзя: у бумаги с путом через полгода это дало бы срок до погашения — не тот
 * срок, к которому посчитан её спред в этой же строке.
 */
export function maturityShort(e) {
  if (!e || e.years == null) return null;
  return `(${fmt.num(e.years, 1)} г)`;
}

/** Полная подпись срока — в подсказку к короткой: к чему считан срок и когда
 *  бумага гасится. Дата тут ПОГАШЕНИЕ, а годы — до горизонта прайсинга, потому
 *  и названы раздельно. */
export function maturityTxt(e) {
  if (e.years == null && !e.maturity) return null;
  const d = e.maturity ? `погашение ${fmt.date(e.maturity)}` : null;
  if (e.years == null) return d;
  const y = e.years < 1
    ? `${Math.round(e.years * 12)} мес`
    : `${fmt.num(e.years, 1)} г`;
  const left = `до расчётной даты (оферта/погашение) ${y}`;
  return d ? `${left} · ${d}` : left;
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

/** Объём события, который показываем человеку: НАКОПЛЕННЫЙ объём — вся глубина
 *  стакана до цены, на которой набор остановился.
 *  money_rub в режиме порога равен самому порогу, а money_ok_rub без границ
 *  спреда — это вся сторона книги; оба оставлены фолбэком для старых событий
 *  ленты, где нового поля ещё нет. */
export function eventMoney(e) {
  if (!e) return null;
  return e.level_money_rub ?? e.money_ok_rub ?? e.money_rub ?? null;
}

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
