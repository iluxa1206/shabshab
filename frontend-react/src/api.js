// Префикс перед /app/ в URL страницы: "" на deskdeskdesk.ru, "/desk" на assetallocator.ru/desk
// (Caddy срезает префикс до проксирования, но браузер должен слать запросы с ним).
// Вычисляется один раз при загрузке; вложенные SPA-пути (/app/funds/X5) регэксп переживает.
const API = location.pathname.replace(/\/app(\/.*)?$/, "");

// Базовый путь SPA для react-router (учитывает возможный префикс перед /app).
export const APP_BASENAME = `${API}/app`;

// Ошибка 401 — сессия истекла/отсутствует. Глобальный onError QueryClient → логин.
export class UnauthorizedError extends Error {
  constructor() { super("unauthorized"); this.name = "UnauthorizedError"; }
}

// Тело ошибки FastAPI: {detail: "..."}. Достаём человекочитаемое сообщение.
async function errText(r, fallback) {
  try { const d = await r.json(); if (d && d.detail) return d.detail; } catch { /* ignore */ }
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

export const fetchFloaterYield = (isin) =>
  request(`/api/curves/floater-yield?isin=${encodeURIComponent(isin)}`);

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

export const fetchBondDetails = (isin) => request(`/api/bonds/${isin}`);

// Динамика спредов: серия DM(флоатер)/g-спред(фикс) по историч. дневным ценам.
export const fetchSpreadHistory = (isin, { kind = "floater", secid, board = "TQCB", days = 120 } = {}) => {
  let u = `/api/history/${encodeURIComponent(isin)}/spread?kind=${kind}&days=${days}&board=${board}`;
  if (secid) u += `&secid=${encodeURIComponent(secid)}`;
  return request(u);
};

export const fetchCandles = (isin, tf = "1d", { secid, board } = {}) => {
  let u = `/api/bonds/${encodeURIComponent(isin)}/candles?tf=${tf}`;
  if (board) u += `&board=${board}`;
  if (secid) u += `&secid=${encodeURIComponent(secid)}`;
  return request(u);
};

// --- Фиксы (ОФЗ-ПД + ликвидные корпораты) ---
export const fetchFixed = () => request("/api/fixed");
export const fetchFixedDetails = (isin) => request(`/api/fixed/${encodeURIComponent(isin)}`);

// Калькулятор карточки фикса: YTM/g-спред/z-спред/дюрация/dirty под произвольную цену.
export const repriceFixed = (isin, price, signal) =>
  request(`/api/fixed/${encodeURIComponent(isin)}/reprice?price=${encodeURIComponent(price)}`, { signal });

// Калькулятор карточки: пересчёт метрик оценки под произвольную чистую цену.
export const repriceBond = (isin, price, signal) =>
  request(`/api/bonds/${encodeURIComponent(isin)}/reprice?price=${encodeURIComponent(price)}`, { signal });

// Стакан выпуска (Alor snapshot): bids/asks с per-level SM/DM/YTM (тот же расчёт,
// что калькулятор карточки, батчем по уровням). full=true — все уровни лестницы.
export const fetchOrderbook = (isin, { depth = 10, full = false, kind = "floater" } = {}, signal) =>
  request(`/api/orderbook/${encodeURIComponent(isin)}?depth=${depth}&full=${full}&kind=${kind}`, { signal });

// --- Алерты по стакану (per-user) ---
export const fetchAlerts = () => request("/api/alerts").then((d) => d.alerts || []);
export const createAlert = (body) => request("/api/alerts", { method: "POST", json: body });
export const updateAlert = (id, patch) => request(`/api/alerts/${id}`, { method: "PATCH", json: patch });
export const deleteAlert = (id) => request(`/api/alerts/${id}`, { method: "DELETE" });

// --- Модуль «Фонды» ---
export const fetchFunds = () => request("/api/funds").then((d) => d.funds || []);

export const createFund = ({ code, name, base_ccy }) =>
  request("/api/funds", { method: "POST", json: { code, name, base_ccy } });

export const patchFund = (code, patch) =>
  request(`/api/funds/${encodeURIComponent(code)}`, { method: "PATCH", json: patch });

export const deleteFund = (code) =>
  request(`/api/funds/${encodeURIComponent(code)}`, { method: "DELETE" });

export const fetchFundSummary = (code) => request(`/api/funds/${encodeURIComponent(code)}/summary`);

export const putFundSnapshot = (code, csv, snapDate) =>
  request(`/api/funds/${encodeURIComponent(code)}/snapshot`, { method: "PUT", json: { csv, snap_date: snapDate || null } });

export const putFundPosition = (code, isin, qty) =>
  request(`/api/funds/${encodeURIComponent(code)}/position`, { method: "PUT", json: { isin, qty } });

export const fetchFundRepos = (code) =>
  request(`/api/funds/${encodeURIComponent(code)}/repo`).then((d) => d.repos || []);

export const addFundRepo = (code, repo) =>
  request(`/api/funds/${encodeURIComponent(code)}/repo`, { method: "POST", json: repo });

export const deleteFundRepo = (code, id) =>
  request(`/api/funds/${encodeURIComponent(code)}/repo/${id}`, { method: "DELETE" });

export const fetchFundCashflow = (code, months = 12) =>
  request(`/api/funds/${encodeURIComponent(code)}/cashflow?months=${months}`);

export const fetchFundScenarios = (code) =>
  request(`/api/funds/${encodeURIComponent(code)}/scenarios`);

export const fetchFundsCalendar = (days = 90) =>
  request(`/api/funds/_meta/calendar?days=${days}`);

export const fetchBenchmarks = (days = 180) =>
  request(`/api/funds/_meta/benchmarks?days=${days}`);

// WebSocket live-цен. onPrice(isin, price). Возвращает {resubscribe, close}.
// resubscribe шлёт diff: subscribe на новые ISIN, unsubscribe на убранные (без дублей).
// Reconnect — экспоненциальный backoff 1с → 30с, сброс при успешном коннекте.
export function connectMarketWs(getIsins, onStatus, onPrice) {
  const WS_URL =
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + API + "/api/ws/market";
  let ws = null;
  let closed = false;
  let reconnectTimer = null;
  let backoff = 1000;
  let subscribed = new Set(); // ISIN, подписанные на текущем соединении

  const send = (obj) => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  };

  // diff желаемых подписок против фактических
  const sync = () => {
    if (!ws || ws.readyState !== 1) return;
    const want = new Set(getIsins());
    for (const isin of want) {
      if (!subscribed.has(isin)) {
        send({ action: "subscribe", channel: "market", isin });
        subscribed.add(isin);
      }
    }
    for (const isin of [...subscribed]) {
      if (!want.has(isin)) {
        send({ action: "unsubscribe", channel: "market", isin });
        subscribed.delete(isin);
      }
    }
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
    ws.onopen = () => { onStatus(true); backoff = 1000; subscribed = new Set(); sync(); };
    ws.onclose = () => { onStatus(false); subscribed = new Set(); scheduleReconnect(); };
    ws.onerror = () => onStatus(false);
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.channel === "market" && msg.isin && msg.data) {
        onPrice(msg.isin, msg.data.last_price_pct);
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
