// Backend access: REST, WebSocket, and the server clock. The server is the only source of truth.

export const tg = window.Telegram?.WebApp ?? null;
const initData = tg?.initData || "";

let clockOffset = 0; // serverTime - localTime, ms
export const serverNow = () => Date.now() + clockOffset;

function syncClock(serverMs, sentAt, receivedAt) {
  if (!serverMs) return;
  clockOffset = serverMs - (sentAt + receivedAt) / 2;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const sentAt = Date.now();
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { Authorization: `tma ${initData}`, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError("Server unreachable", 0);
  }
  const receivedAt = Date.now();
  const data = await res.json().catch(() => ({}));
  syncClock(data.server_now, sentAt, receivedAt);
  if (!res.ok) throw new ApiError(data.detail || `Error ${res.status}`, res.status);
  return data;
}

// Realtime channel with automatic reconnect; onMessage gets every server event.
export function connectRealtime(onMessage, onState) {
  let ws = null;
  let delay = 1000;
  let closedByUs = false;

  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/ws?auth=${encodeURIComponent(initData)}`);
    ws.onopen = () => { delay = 1000; onState(true); };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      // WS has no round-trip info; only correct large drifts.
      if (msg.server_now && Math.abs(msg.server_now - serverNow()) > 1500) clockOffset = msg.server_now - Date.now();
      onMessage(msg);
    };
    ws.onclose = (ev) => {
      onState(false);
      if (closedByUs || ev.code === 4401) return;
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 15000);
    };
    ws.onerror = () => ws.close();
  };
  open();
  return () => { closedByUs = true; ws?.close(); };
}

export function haptic(kind = "light") {
  try {
    if (kind === "success" || kind === "error" || kind === "warning") tg?.HapticFeedback?.notificationOccurred(kind);
    else tg?.HapticFeedback?.impactOccurred(kind);
  } catch { /* not in Telegram */ }
}
