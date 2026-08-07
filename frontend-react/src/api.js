// Префикс перед /app/ в URL страницы: "" на deskdeskdesk.ru, "/desk" на assetallocator.ru/desk
// (Caddy срезает префикс до проксирования, но браузер должен слать запросы с ним).
// Вычисляется один раз при загрузке; вложенные SPA-пути (/app/funds/X5) регэксп переживает.
const API = location.pathname.replace(/\/app(\/.*)?$/, "");

// Базовый путь SPA для react-router (учитывает возможный префикс перед /app).
export const APP_BASENAME = `${API}/app`;

// Ссылка на страницу выпуска на cbonds. С Cbonds ID — прямая; иначе — поиск
// cbonds по ISIN (тоже остаётся на cbonds, находит выпуск).
export const cbondsUrl = (isin, cbondsId) =>
  cbondsId ? `https://cbonds.ru/bonds/${cbondsId}/`
           : `https://cbonds.ru/bonds/?isin=${encodeURIComponent(isin || "")}`;

// Ошибка 401 — сессия истекла/отсутствует. Глобальный onError QueryClient → логин.
export class UnauthorizedError extends Error {
  constructor() { super("unauthorized"); this.name = "UnauthorizedError"; }
}

// Тело ошибки FastAPI. detail бывает строкой ({detail:"..."}) ИЛИ массивом
// объектов валидации ({detail:[{loc,msg,type},…]}). Массив без разбора →
// «[object Object]» в UI, поэтому вытягиваем .msg и склеиваем.
async function errText(r, fallback) {
  try {
    const d = await r.json();
    const det = d?.detail;
    if (typeof det === "string") return det;
    if (Array.isArray(det)) {
      const msgs = det.map((e) => {
        const field = Array.isArray(e?.loc) ? e.loc[e.loc.length - 1] : null;
        return (field ? `${field}: ` : "") + (e?.msg || JSON.stringify(e));
      });
      if (msgs.length) return msgs.join("; ");
    } else if (det && typeof det === "object") {
      return det.msg || JSON.stringify(det);
    }
  } catch { /* ignore */ }
  return fallback || `Ошибка (${r.status})`;
}

// Единая точка HTTP: префикс, cookie-сессия, JSON-тело, разбор ошибок.
// 401 → UnauthorizedError; прочие не-2xx → Error с detail из JSON.
async function request(path, { method = "GET", json, signal } = {}) {
  const opts = { method, credentials: "same-origin", signal };
  if (json !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(json);
  }
  const r = await fetch(`${API}${path}`, opts);
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error(await errText(r));
  return r.status === 204 ? null : r.json();
}

// --- Аутентификация ---
export async function login(email, password) {
  try {
    return await request("/api/auth/login", { method: "POST", json: { email, password } });
  } catch (e) {
    if (e instanceof UnauthorizedError) throw new Error("Неверный email или пароль");
    throw e;
  }
}

export async function logout() {
  try { await request("/api/auth/logout", { method: "POST" }); } catch { /* всё равно на логин */ }
}

export const fetchMe = () => request("/api/auth/me");

// Смена своего пароля (любой авторизованный).
export const changePassword = (currentPassword, newPassword) =>
  request("/api/auth/password", { method: "POST", json: { current_password: currentPassword, new_password: newPassword } });

// --- Управление пользователями (только admin) ---
export const adminListUsers = () => request("/api/auth/users").then((d) => d.users || []);

export const adminCreateUser = (email, password, role) =>
  request("/api/auth/users", { method: "POST", json: { email, password, role } });

export const adminUpdateUser = (email, patch) =>
  request(`/api/auth/users/${encodeURIComponent(email)}`, { method: "PATCH", json: patch });

export const adminDeleteUser = (email) =>
  request(`/api/auth/users/${encodeURIComponent(email)}`, { method: "DELETE" });

// --- Реестр инструментов (admin): ревью новых бумаг + ручной ввод параметров ---
export const fetchUnreviewedInstruments = () => request("/api/instruments/unreviewed");

export const fetchInstrument = (isin) =>
  request(`/api/instruments/${encodeURIComponent(isin)}`);

export const setInstrumentParams = (isin, params) =>
  request(`/api/instruments/${encodeURIComponent(isin)}`, { method: "POST", json: params });

export const markInstrumentReviewed = (isin) =>
  request(`/api/instruments/${encodeURIComponent(isin)}/reviewed`, { method: "POST" });

// Сброс ручной правки: снять lock + обнулить явные поля спеки фиксинга —
// спека дальше от авто-источников (bondresearch > парсер > калибратор)
export const resetInstrumentManual = (isin) =>
  request(`/api/instruments/${encodeURIComponent(isin)}/reset-manual`, { method: "POST" });

// Пересчёт бэктеста спеки (лаг/окно vs факт выплат) после правки
export const recheckInstrumentSpec = (isin) =>
  request(`/api/instruments/${encodeURIComponent(isin)}/recheck-spec`, { method: "POST" });

// Разбор текста формулы купона → {parsed:{base,margin_bps,coupon_mode,cap_pct,floor_pct,...}}
export const parseCouponFormula = (formula) =>
  request("/api/instruments/parse-formula", { method: "POST", json: { formula } });

// --- Справочник инструментов (admin): все параметры + импорт/экспорт xlsx ---
const _catalogQuery = (opts = {}) => {
  const q = new URLSearchParams();
  if (opts.floatersOnly) q.set("floaters_only", "true");
  if (opts.onlyActive === false) q.set("only_active", "false");
  const s = q.toString();
  return s ? `?${s}` : "";
};

export const fetchCatalog = (opts = {}) =>
  request(`/api/instruments/catalog${_catalogQuery(opts)}`);

// Прямой URL выгрузки xlsx (cookie same-origin → работает как обычная ссылка/скачивание).
export const catalogExportUrl = (opts = {}) =>
  `${API}/api/instruments/catalog/export${_catalogQuery(opts)}`;

export async function importCatalogXlsx(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${API}/api/instruments/catalog/import`, {
    method: "POST", credentials: "same-origin", body: fd });
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

// --- Мета/кривые/бонды ---
export const fetchMeta = () => request("/api/meta");

// Страница СТАТУС: подключения + полнота прогрева данных + таймстемпы.
export const fetchStatus = () => request("/api/status");

export const fetchCurvePlot = (type) => request(`/api/curves/plot?type=${type}`);

export const fetchKsPath = (series = "ks") => request(`/api/curves/ks-path?series=${series}`);

// Индекс RUONIA по дням: ставка ЦБ, официальный индекс (публикуется с 2010-01-11)
// и наш расчётный на тех же ставках — сверка нашей механики с эталоном.
export const fetchRuoniaIndex = (days = 400) => request(`/api/curves/ruonia-index?days=${days}`);

export function fetchBonds({ withVal, universe, extra, signal }) {
  let url;
  if (universe) {
    url = `/api/bonds?universe=true&limit=2000`;
    // watchlist (extra) обогащается live-ценой/dirty/DM/купоном на бэке
    if (extra && extra.length) url += `&extra=${encodeURIComponent(extra.join(","))}`;
  } else {
    url = `/api/bonds?with_market=true&with_valuation=${withVal}&limit=500`;
    if (extra && extra.length) url += `&extra=${encodeURIComponent(extra.join(","))}`;
  }
  return request(url, { signal });
}

// Лестницы стаканов по всему юниверсу (фоновый снимок Alor) — сырьё для фильтра
// по объёму: VWAP на тикет считает фронт (src/vwap.js).
export const fetchDepth = () => request("/api/orderbook/depth/all");

export const fetchBondDetails = (isin) => request(`/api/bonds/${isin}`);

// Строка списка по одной бумаге (рейтинг, эмитент, Y-IDX, DM, спред-дюрация) —
// того в /api/bonds/{isin} нет. extra тянет любой ISIN вне базового списка;
// limit=1 держит ответ маленьким, нужную строку выбираем по ISIN.
export const fetchBondRow = async (isin) => {
  const r = await request(`/api/bonds?with_market=true&with_valuation=true&limit=1`
    + `&extra=${encodeURIComponent(isin)}`);
  return (r.items || []).find((b) => b.isin === isin) || null;
};

// Динамика медианного Y-IDX по рейтинг-бакетам/топ-эмитентам (вкладка АНАЛИТИКА).
// isins — отфильтрованный набор таблицы: график согласован с фильтрами дашборда.
export const fetchYidxHistory = (days, by, isins, signal) =>
  request(`/api/history/aggregate/yidx`, { method: "POST", json: { days, by, isins }, signal });

// Паспорт бумаги: провенанс всех данных + бэктест спеки + waterfall PV + чеки.
export const fetchBondAudit = (isin) =>
  request(`/api/bonds/${encodeURIComponent(isin)}/audit`);

// Дневная раскладка фиксинга: все будущие купоны, ставка индекса на каждый день.
export const fetchCouponDays = (isin) =>
  request(`/api/bonds/${encodeURIComponent(isin)}/coupon-days`);

// Динамика спредов: серия DM(флоатер)/g-спред(фикс) по историч. дневным ценам.
// board не задаём: бэкенд сам резолвит тикер/борд по ISIN (ОФЗ = SU29…@TQOB,
// риск-сектор = TQRD) — прибитый TQCB отдавал по ним пустую историю.
// from (опц., ISO-дата) — календарная граница окна вместо числа торговых дней:
// так график спреда показывает ТО ЖЕ окно, что график цены в карточке.
export const fetchSpreadHistory = (isin, { kind = "floater", secid, board, days = 120, from } = {}) => {
  let u = `/api/history/${encodeURIComponent(isin)}/spread?kind=${kind}&days=${days}`;
  if (from) u += `&from=${from}`;
  if (board) u += `&board=${board}`;
  if (secid) u += `&secid=${encodeURIComponent(secid)}`;
  return request(u);
};

// Честная динамика спредов (флоатер): каждый день — свой calc_date/кривая/НКД.
export const fetchSpreadHonest = (isin, { days = 120, board } = {}) =>
  request(`/api/history/${encodeURIComponent(isin)}/spread_honest?days=${days}`
          + (board ? `&board=${board}` : ""));

// Калькулятор прошлых периодов: (дата, цена) → метрики как-на-дату.
export const fetchRepricePast = (isin, { date, price, board } = {}) => {
  let u = `/api/history/${encodeURIComponent(isin)}/reprice?date=${date}`;
  if (board) u += `&board=${board}`;
  if (price != null) u += `&price=${price}`;
  return request(u);
};

// Часовые бары: средневзвешенная цена часа (VWAP) + спред по ней + стороны
// сделок (buy/sell VWAP из тикового архива). hours>1 склеивает часы на бэке.
export const fetchHourlyBars = (isin, { kind = "floater", days = 30, hours = 1,
                                        board, refresh = true } = {}) => {
  let u = `/api/history/${encodeURIComponent(isin)}/bars?kind=${kind}&days=${days}&hours=${hours}`;
  if (board) u += `&board=${board}`;
  if (!refresh) u += "&refresh=false";
  return request(u);
};

// Сделки из тикового архива. min_value (₽) отсекает мелочь — остаются крупные
// принты. Глубина: у брокера ~30 дней, глубже — только то, что накопил демон.
// order='value' — самые крупные за окно (лента маркеров), 'ts' — последние.
export const fetchTrades = (isin, { days = 30, minValue = 0, side, limit = 500,
                                    order, refresh = true } = {}) => {
  let u = `/api/history/${encodeURIComponent(isin)}/trades?days=${days}&min_value=${minValue}&limit=${limit}`;
  if (order) u += `&order=${order}`;
  if (side) u += `&side=${side}`;
  if (!refresh) u += "&refresh=false";
  return request(u);
};

// Общерыночная лента сделок (вкладка СДЕЛКИ): тот же архив, но по всем бумагам.
// Онлайн-дрейна тут нет — данные до последнего прогона часового демона.
export const fetchMarketTape = ({ days = 1, minValue = 0, side, issuer, isin,
                                  limit = 500 } = {}, signal) => {
  const p = new URLSearchParams({ days, min_value: minValue, limit });
  if (side) p.set("side", side);
  // эмитенты — повторяющийся параметр; пустой массив не шлём (иначе бэк поймёт
  // это как «фильтр задан, но ничего не подошло» и вернёт пустую ленту)
  for (const e of [].concat(issuer || [])) if (e) p.append("issuer", e);
  if (isin) p.set("isin", isin);
  return request(`/api/trades?${p}`, { signal });
};

export const fetchTapeIssuers = () =>
  request("/api/trades/issuers").then((d) => d.issuers || []);

export const fetchCandles = (isin, tf = "1d", { secid, board } = {}) => {
  let u = `/api/bonds/${encodeURIComponent(isin)}/candles?tf=${tf}`;
  if (board) u += `&board=${board}`;
  if (secid) u += `&secid=${encodeURIComponent(secid)}`;
  return request(u);
};

// --- Фиксы (ОФЗ-ПД + ликвидные корпораты) ---
// календарь выплат юниверса (купоны/погашения, ₽ на бумагу); from/to = ISO-даты
export const fetchPaymentsCalendar = ({ from, to } = {}) => {
  const p = new URLSearchParams();
  if (from) p.set("from", from);
  if (to) p.set("to", to);
  const q = p.toString();
  return request(`/api/bonds/calendar${q ? "?" + q : ""}`);
};

export const fetchFixed = () => request("/api/fixed");
export const fetchFixedDetails = (isin) => request(`/api/fixed/${encodeURIComponent(isin)}`);

// Калькулятор кастомной облигации: метрики по введённым параметрам (купон/
// частота/погашение/цена/номинал), кривая и calc_date — как у вкладки ФИКСЫ.
export const calcCustomBond = ({ coupon, freq, maturity, price, face }, signal) => {
  const p = new URLSearchParams({ coupon_pct: coupon, freq, maturity, price });
  if (face) p.set("face", face);
  return request(`/api/calc/custom?${p}`, { signal });
};

// Калькулятор карточки фикса: YTM/g-спред/z-спред/дюрация/dirty под произвольную цену.
export const repriceFixed = (isin, price, signal) =>
  request(`/api/fixed/${encodeURIComponent(isin)}/reprice?price=${encodeURIComponent(price)}`, { signal });

// Калькулятор карточки: пересчёт метрик оценки под произвольную чистую цену.
export const repriceBond = (isin, price, signal) =>
  request(`/api/bonds/${encodeURIComponent(isin)}/reprice?price=${encodeURIComponent(price)}`, { signal });

// Обратная задача: целевой спред Y-IDX (bps) → чистая цена + метрики под ней
// (clean_price_pct — найденная цена)
export const priceFromSpread = (isin, yIdx, signal) =>
  request(`/api/bonds/${encodeURIComponent(isin)}/price_from_spread?y_idx=${encodeURIComponent(yIdx)}`, { signal });

// Стакан выпуска (Alor snapshot): bids/asks с per-level SM/DM/YTM (тот же расчёт,
// что калькулятор карточки, батчем по уровням). full=true — все уровни лестницы.
export const fetchOrderbook = (isin, { depth = 10, full = false, kind = "floater" } = {}, signal) =>
  request(`/api/orderbook/${encodeURIComponent(isin)}?depth=${depth}&full=${full}&kind=${kind}`, { signal });

// --- Алерты по стакану (per-user) ---
export const fetchAlerts = () => request("/api/alerts").then((d) => d.alerts || []);
export const createAlert = (body) => request("/api/alerts", { method: "POST", json: body });
export const updateAlert = (id, patch) => request(`/api/alerts/${id}`, { method: "PATCH", json: patch });
export const deleteAlert = (id) => request(`/api/alerts/${id}`, { method: "DELETE" });

// Котировки всего рынка одним запросом (цена, верх стакана, средневзвес дня,
// оборот) — тянутся тактом 5с для бумаг вне избранного. По избранному те же
// поля приходят push'ем через WS и авторитетнее.
export const fetchQuotes = (signal) => request("/api/bonds/quotes", { signal });

// WebSocket live-котировок. onQuote(isin, data), где data — частичный патч
// строки: {last_price_pct, bid, ask, bid_qty, ask_qty, vwap_pct, vwap_volume}.
// Приходит push'ем от Alor по избранному: и котировка, и сделка двигают строку.
// Возвращает {resubscribe, close}. resubscribe шлёт diff: subscribe на новые
// ISIN, unsubscribe на убранные (без дублей).
// Reconnect — экспоненциальный backoff 1с → 30с, сброс при успешном коннекте.
export function connectMarketWs(getIsins, onStatus, onQuote) {
  const WS_URL =
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + API + "/api/ws/market";
  let ws = null;
  let closed = false;
  let reconnectTimer = null;
  let backoff = 1000;
  let subscribedAll = false;  // wildcard-подписка текущего соединения

  const send = (obj) => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  };

  // Вся таблица живая: одна wildcard-подписка вместо диффа списка избранного —
  // бэк пушит патчи всех бумаг юниверса, фронт коалесцирует и мерджит.
  const sync = () => {
    if (!ws || ws.readyState !== 1 || subscribedAll) return;
    send({ action: "subscribe", channel: "market", isin: "*" });
    subscribedAll = true;
  };

  const scheduleReconnect = () => {
    if (closed || reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; open(); }, backoff);
    backoff = Math.min(backoff * 2, 30000);
  };

  const open = () => {
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      onStatus(false);
      scheduleReconnect();
      return;
    }
    ws.onopen = () => { onStatus(true); backoff = 1000; subscribedAll = false; sync(); };
    ws.onclose = () => { onStatus(false); subscribedAll = false; scheduleReconnect(); };
    ws.onerror = () => onStatus(false);
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.channel === "market" && msg.isin && msg.data) {
        onQuote(msg.isin, msg.data);
      }
    };
  };
  open();

  return {
    resubscribe: sync,
    close() {
      closed = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      if (ws) ws.close();
    },
  };
}

// Реал-тайм стакан по одной бумаге через WS (канал orderbook). onData(payload)
// — payload = {orderbook:{bids,asks}, pricing_status, warnings}. Reconnect с
// backoff. Питается фоновым Alor-WS клиентом бэка; фолбэк — HTTP-поллинг.
export function connectOrderbookWs(isin, onData) {
  const WS_URL =
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + API + "/api/ws/market";
  let ws = null, closed = false, reconnectTimer = null, backoff = 1000;
  const send = (o) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };
  const scheduleReconnect = () => {
    if (closed || reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; open(); }, backoff);
    backoff = Math.min(backoff * 2, 30000);
  };
  const open = () => {
    try { ws = new WebSocket(WS_URL); } catch { scheduleReconnect(); return; }
    ws.onopen = () => { backoff = 1000; send({ action: "subscribe", channel: "orderbook", isin }); };
    ws.onclose = () => scheduleReconnect();
    ws.onerror = () => {};
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.channel === "orderbook" && msg.isin === isin && msg.data) onData(msg.data);
    };
  };
  open();
  return {
    close() {
      closed = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      try { send({ action: "unsubscribe", channel: "orderbook", isin }); } catch { /* ignore */ }
      if (ws) ws.close();
    },
  };
}
