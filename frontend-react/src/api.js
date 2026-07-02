const API = ""; // same origin

export async function fetchMeta() {
  const r = await fetch(`${API}/api/meta`);
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
  const r = await fetch(url, { signal });
  if (!r.ok) throw new Error("bonds " + r.status);
  return r.json();
}

export async function searchBonds(q, signal) {
  const r = await fetch(`${API}/api/bonds/search?q=${encodeURIComponent(q)}`, { signal });
  if (!r.ok) throw new Error("search " + r.status);
  return (await r.json()).items || [];
}

export async function fetchBondDetails(isin) {
  const r = await fetch(`${API}/api/bonds/${isin}`);
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
