// Торговое окно MOEX на фронте — зеркало api/main._in_moex_trading_hours:
// пн–пт, 07:00–23:50 МСК (утренняя + основная + вечерняя сессии).
// Нужно поллингу графиков: вне сессии данные не меняются, и опрашивать бэк
// (а он — ISS и Alor) всю ночь незачем.

// Время МСК считаем через Intl, а не через смещение +3: у пользователя может
// стоять любая таймзона, а у сервера MOEX — своя, и вычитание часов руками
// врёт на машинах с летним временем.
const MSK = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Moscow", weekday: "short", hour: "2-digit", minute: "2-digit",
  hour12: false,
});

export function moscowNow(now = new Date()) {
  const p = Object.fromEntries(MSK.formatToParts(now).map((x) => [x.type, x.value]));
  return { weekday: p.weekday, hour: Number(p.hour), minute: Number(p.minute) };
}

export function inTradingHours(now = new Date()) {
  const { weekday, hour, minute } = moscowNow(now);
  if (weekday === "Sat" || weekday === "Sun") return false;
  const m = hour * 60 + minute;
  return m >= 7 * 60 && m <= 23 * 60 + 50;
}

// Значение для refetchInterval react-query: интервал в торговые часы, false —
// вне их (react-query так полностью выключает таймер).
export const liveInterval = (ms) => () => (inTradingHours() ? ms : false);
