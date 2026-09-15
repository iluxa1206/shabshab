// Валюта НОМИНАЛА выпуска. SUR/RUR — старые обозначения рубля в ISS,
// CNH приводим к коду, которым подписаны остальные экраны и ЦБ.
export const DEFAULT_FIXED_CURRENCIES = ["RUB"];

export function fixedCurrency(row) {
  const raw = String(row?.face_unit || row?.faceunit || "RUB").trim().toUpperCase();
  if (!raw || raw === "RUB" || raw === "SUR" || raw === "RUR") return "RUB";
  return raw === "CNH" ? "CNY" : raw;
}

export function isDefaultFixedCurrencySelection(sel) {
  return sel.length === 1 && sel[0] === "RUB";
}
