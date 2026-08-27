// VWAP по лестнице стакана на заданный размер тикета + Y-IDX по этой цене.
//
// Зачем: верх стакана — это цена на одну заявку, часто на 50 бумаг. Трейдеру
// нужна цена, по которой реально исполнится миллион, и спред К ЭТОЙ ЦЕНЕ.
// Лестницы приходят из /api/orderbook/depth/all (фоновый снимок Alor), объём
// задаёт пользователь — поэтому считаем на фронте, а не на бэке.
//
// Деньги уровня — ГРЯЗНЫЕ (реальная сумма расчётов):
//   money = qty × (номинал × цена% / 100 + НКД)
// номинал и НКД берём из строки таблицы (face_value_rub / accrued_rub) — это
// ровно те числа, из которых бэк собрал dirty_price_rub: амортизация учтена,
// НКД на дату поставки T+1.

// Деньги одного уровня стакана, ₽.
export function levelMoney(pxPct, qty, face, accrued) {
  if (pxPct == null || qty == null || !face) return 0;
  return qty * (face * pxPct / 100 + (accrued || 0));
}

// Средневзвешенная цена набора volRub рублей по лестнице levels ([[цена%, qty], …],
// отсортированной от лучшей цены). Последний уровень берётся частично — ровно на
// остаток тикета. Возвращает {px, money, levels, partial:false} либо, если всей
// глубины не хватает, {px, money, levels, partial:true} с VWAP по ВСЕЙ книге —
// вызывающий решает, показывать такую строку или прятать.
export function vwapFor(levels, volRub, face, accrued) {
  if (!Array.isArray(levels) || !levels.length || !(volRub > 0) || !face) return null;
  let left = volRub, cost = 0, taken = 0, used = 0;
  for (const [px, qty] of levels) {
    const money = levelMoney(px, qty, face, accrued);
    if (money <= 0) continue;
    used += 1;
    const part = Math.min(money, left);
    cost += part * px;          // взвешиваем цену деньгами, не количеством
    taken += part;
    left -= part;
    if (left <= 1e-9) break;
  }
  if (taken <= 0) return null;
  return { px: cost / taken, money: taken, levels: used, partial: left > 1e-9 };
}

// Допуск по объёму: набранное принимается за требуемое, если добрали ≥90%
// запрошенного. Заявка «100 000 бумаг по 98» даёт ~98 млн ₽ грязными и по
// строгому порогу «100 млн» вылетала бы, хотя это ровно тот тикет, который
// трейдер искал. Округление цены/номинала/НКД тут же — в пределах допуска.
export const VOL_TOL = 0.9;

// Набрала ли сторона объём с учётом допуска. Полный набор — всегда да; частичный
// — да, если добрали ≥ VOL_TOL×требуемого (VWAP тогда по всей книге).
function passes(v, want) {
  if (!v) return false;
  return !v.partial || v.money >= want * VOL_TOL;
}

// Строка таблицы + лестница → строка с котировками НА ОБЪЁМ по сторонам.
// volBid / volAsk — требуемые объёмы (₽) на биде и оффере, 0 = сторона не
// фильтруется (её котировка остаётся верхом стакана). mode — как складывать
// условия, когда заполнены ОБА поля: "and" — обе стороны должны набрать объём,
// "or" — достаточно одной. Возвращает null, если условие не выполнено (строку
// прячем: тикет не исполнить). Заполненная сторона, не набравшая объём, гаснет
// в прочерк — в режиме "or" строка может остаться живой за счёт второй стороны.
export function applyVolume(b, ladder, volBid, volAsk, mode = "and") {
  const wantBid = volBid > 0, wantAsk = volAsk > 0;
  if (!wantBid && !wantAsk) return b;
  if (!ladder) return null;
  const face = b.face_value_rub, acc = b.accrued_rub;
  const bid = wantBid ? vwapFor(ladder.b, volBid, face, acc) : null;
  const ask = wantAsk ? vwapFor(ladder.a, volAsk, face, acc) : null;
  const okBid = passes(bid, volBid), okAsk = passes(ask, volAsk);
  const pass = wantBid && wantAsk
    ? (mode === "or" ? okBid || okAsk : okBid && okAsk)
    : (wantBid ? okBid : okAsk);
  if (!pass) return null;
  const px = (v) => Math.round(v.px * 10000) / 10000;
  return {
    ...b,
    // в подпись кладём ФАКТИЧЕСКИ набранные деньги (при частичном наборе в
    // пределах допуска они меньше запрошенных) — VWAP посчитан именно по ним
    _vwap_bid: okBid ? bid.money : null,
    _vwap_ask: okAsk ? ask.money : null,
    _vwap_bid_levels: okBid ? bid.levels : null,
    _vwap_ask_levels: okAsk ? ask.levels : null,
    // СПРЕД набора — только с бэкенда (y_idx_vol_*_bps): его считает движок по
    // методике на размер тикета. В браузере он выводился наклоном dY/dP — линия
    // уводила число вслед за уехавшим якорем (27.08.2026). Нет числа — прочерк.
    //
    // ЦЕНА набора — бэковская, если она пришла (тогда пара «цена → спред»
    // заведомо из одного расчёта), иначе своя. Своя честна: это арифметика
    // книги, а не модель, и она уже посчитана здесь ради самого фильтра.
    // Без этого фолбэка цена пропадала бы у всех, по кому движок ещё не
    // проходил, — а у застывшего неликвида повода напомнить о себе может не
    // быть весь день.
    bid_price_pct: wantBid ? (okBid ? (b.vol_bid_price_pct ?? px(bid)) : null) : b.bid_price_pct,
    ask_price_pct: wantAsk ? (okAsk ? (b.vol_ask_price_pct ?? px(ask)) : null) : b.ask_price_pct,
    y_idx_bid_bps: wantBid ? (okBid ? (b.y_idx_vol_bid_bps ?? null) : null) : b.y_idx_bid_bps,
    y_idx_ask_bps: wantAsk ? (okAsk ? (b.y_idx_vol_ask_bps ?? null) : null) : b.y_idx_ask_bps,
  };
}
