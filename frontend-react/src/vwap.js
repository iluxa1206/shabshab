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

// Y-IDX по произвольной цене px: линейно от известного якоря (Y-IDX верха стакана
// или последней сделки) через производную dY/dP (y_idx_slope_bps_per_pct с бэка).
// На масштабе стакана (доли пп) Y-IDX(цена) практически прямая — ошибка
// линеаризации заметно меньше bps. Без наклона или якоря — null, а не догадка.
export function yIdxAt(b, px, side) {
  const k = b.y_idx_slope_bps_per_pct;
  if (px == null || k == null) return null;
  const anchors = side === "ask"
    ? [[b.ask_price_pct, b.y_idx_ask_bps], [b.bid_price_pct, b.y_idx_bid_bps],
       [b.last_price_pct, b.yield_over_index_bps]]
    : [[b.bid_price_pct, b.y_idx_bid_bps], [b.ask_price_pct, b.y_idx_ask_bps],
       [b.last_price_pct, b.yield_over_index_bps]];
  for (const [ap, ay] of anchors) {
    if (ap != null && ay != null) return Math.round(ay + (px - ap) * k);
  }
  return null;
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
  const okBid = !!bid && !bid.partial, okAsk = !!ask && !ask.partial;
  const pass = wantBid && wantAsk
    ? (mode === "or" ? okBid || okAsk : okBid && okAsk)
    : (wantBid ? okBid : okAsk);
  if (!pass) return null;
  return {
    ...b,
    _vwap_bid: okBid ? volBid : null,
    _vwap_ask: okAsk ? volAsk : null,
    _vwap_bid_levels: okBid ? bid.levels : null,
    _vwap_ask_levels: okAsk ? ask.levels : null,
    bid_price_pct: wantBid ? (okBid ? Math.round(bid.px * 10000) / 10000 : null) : b.bid_price_pct,
    ask_price_pct: wantAsk ? (okAsk ? Math.round(ask.px * 10000) / 10000 : null) : b.ask_price_pct,
    y_idx_bid_bps: wantBid ? (okBid ? yIdxAt(b, bid.px, "bid") : null) : b.y_idx_bid_bps,
    y_idx_ask_bps: wantAsk ? (okAsk ? yIdxAt(b, ask.px, "ask") : null) : b.y_idx_ask_bps,
  };
}
