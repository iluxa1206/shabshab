// Валюта НОМИНАЛА выпуска. SUR/RUR — старые обозначения рубля в ISS,
// CNH приводим к коду, которым подписаны остальные экраны и ЦБ.
export const DEFAULT_FIXED_CURRENCIES = ["RUB"];

export function fixedCurrency(row) {
  const raw = String(row?.face_unit || row?.faceunit || "RUB").trim().toUpperCase();
  if (!raw || raw === "RUB" || raw === "SUR" || raw === "RUR") return "RUB";
  return raw === "CNH" ? "CNY" : raw;
}

// Y-IDX у бумаги не определён ПО СМЫСЛУ: валютный номинал (база — рублёвая RUONIA)
// или бессрочная (рента). Это не «данных нет», а «такого числа не бывает» — в ячейке
// «н/о» с причиной, а не прочерк, который читается как поломка.
export function yidxUndefinedReason(row) {
  if (fixedCurrency(row) !== "RUB") return "валютный номинал: база Y-IDX — рублёвая RUONIA, спред не определён; смотрите YTM";
  if (row?.perpetual) return "бессрочная бумага: доходность — рента, спред к RUONIA не определён";
  return null;
}

export function isDefaultFixedCurrencySelection(sel) {
  return sel.length === 1 && sel[0] === "RUB";
}
