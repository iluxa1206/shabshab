// Валюта отображения модуля «Фонды»: все ₽-метрики бэка делятся на курс TOM.
// Курсы приходят в summary.fx (бэк: MOEX TOM → фолбэк ЦБ, см. services/fx.py).
import { fmt, DASH } from "../../format.js";

export const CCYS = ["RUB", "USD", "CNY"];
export const CCY_SIGN = { RUB: "₽", USD: "$", CNY: "¥" };

// курс валюты отображения; null = курса нет (показываем рубли)
export function dispRate(dispCcy, rates) {
  if (dispCcy === "RUB") return 1;
  const r = rates?.[dispCcy];
  return r > 0 ? r : null;
}

export function conv(v, rate) {
  return v == null || rate == null ? v : v / rate;
}

// «21,1 млн ₽» / «0,27 млн $»; d подбирается: валютные суммы мельче
export function mlnCcy(v, rate, sign, d) {
  if (v == null) return DASH;
  const x = conv(v, rate) / 1e6;
  const digits = d != null ? d : Math.abs(x) < 10 ? 2 : 1;
  return fmt.num(x, digits) + " млн " + sign;
}

export function numCcy(v, rate, sign, d = 0) {
  if (v == null) return DASH;
  return fmt.num(conv(v, rate), d) + " " + sign;
}
