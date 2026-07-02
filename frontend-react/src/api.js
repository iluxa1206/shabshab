const API = ""; // same origin

// Ошибка 401 — сессия истекла/отсутствует. App перехватывает и показывает логин.
export class UnauthorizedError extends Error {
  constructor() { super("unauthorized"); this.name = "UnauthorizedError"; }
}

// fetch с cookie-сессией; 401 → UnauthorizedError (единый гейт авторизации).
async function authFetch(url, opts = {}) {
  const r = await fetch(url, { credentials: "same-origin", ...opts });
  if (r.status === 401) throw new UnauthorizedError();
  return r;
}

export async function login(email, password) {
  const r = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) {
    const msg = r.status === 401 ? "Неверный email или пароль" : `Ошибка входа (${r.status})`;
    throw new Error(msg);
  }
  return r.json();
}

export async function logout() {
  await fetch(`${API}/api/auth/logout`, { method: "POST", credentials: "same-origin" });
}

export async function fetchMe() {
  const r = await fetch(`${API}/api/auth/me`, { credentials: "same-origin" });
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error("me " + r.status);
  return r.json();
}

// Тело ошибки FastAPI: {detail: "..."}. Достаём человекочитаемое сообщение.
async function errText(r, fallback) {
  try { const d = await r.json(); if (d && d.detail) return d.detail; } catch { /* ignore */ }
  return fallback || `Ошибка (${r.status})`;
}

async function jsonOrThrow(r) {
  if (r.status === 401) throw new UnauthorizedError();
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

// Смена своего пароля (любой авторизованный).
export async function changePassword(currentPassword, newPassword) {
  const r = await authFetch(`${API}/api/auth/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  return jsonOrThrow(r);
}

// --- Управление пользователями (только admin) ---
export async function adminListUsers() {
  const r = await authFetch(`${API}/api/auth/users`);
  return (await jsonOrThrow(r)).users || [];
}

export async function adminCreateUser(email, password, role) {
  const r = await authFetch(`${API}/api/auth/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, role }),
  });
  return jsonOrThrow(r);
}

export async function adminUpdateUser(email, patch) {
  const r = await authFetch(`${API}/api/auth/users/${encodeURIComponent(email)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return jsonOrThrow(r);
}

export async function adminDeleteUser(email) {
  const r = await authFetch(`${API}/api/auth/users/${encodeURIComponent(email)}`, {
    method: "DELETE",
  });
  return jsonOrThrow(r);
}

export async function fetchMeta() {
  const r = await authFetch(`${API}/api/meta`);
  if (!r.ok) throw new Error("meta " + r.status);
  return r.json();
}

export async function fetchBonds({ withVal, withNrd, universe, extra, signal }) {
  let url;
  if (universe) {
    url = `${API}/api/bonds?universe=true&limit=2000`;
    // watchlist (extra) обогащается live-ценой/dirty/DM/купоном на бэке
    if (extra && extra.length) url += `&extra=${encodeURIComponent(extra.join(","))}`;
  } else {
    url = `${API}/api/bonds?with_market=true&with_valuation=${withVal}&with_nrd=${withNrd}&limit=500`;
    if (extra && extra.length) url += `&extra=${encodeURIComponent(extra.join(","))}`;
  }
  const r = await authFetch(url, { signal });
  if (!r.ok) throw new Error("bonds " + r.status);
  return r.json();
}

export async function searchBonds(q, signal) {
  const r = await authFetch(`${API}/api/bonds/search?q=${encodeURIComponent(q)}`, { signal });
  if (!r.ok) throw new Error("search " + r.status);
  return (await r.json()).items || [];
}

export async function fetchBondDetails(isin) {
  const r = await authFetch(`${API}/api/bonds/${isin}`);
  if (!r.ok) throw new Error("details " + r.status);
  return r.json();
}

// WebSocket live-цен. onPrice(isin, price). Возвращает {close}.
export function connectMarketWs(getIsins, onStatus, onPrice) {
  const WS_URL =
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws/market";
  let ws;
  let closed = false;
  let reconnectTimer = null;

  const subscribeAll = () => {
    if (!ws || ws.readyState !== 1) return;
    for (const isin of getIsins()) {
      ws.send(JSON.stringify({ action: "subscribe", channel: "market", isin }));
    }
  };

  const open = () => {
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      onStatus(false);
      return;
    }
    ws.onopen = () => { onStatus(true); subscribeAll(); };
    ws.onclose = () => {
      onStatus(false);
      if (!closed) reconnectTimer = setTimeout(open, 4000);
    };
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
    resubscribe: subscribeAll,
    close() {
      closed = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      if (ws) ws.close();
    },
  };
}
