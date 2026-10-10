// App shell: Telegram integration, shared state, tabs, realtime sync.
import { ApiError, api, connectRealtime, haptic, tg } from "./api.js";
import { renderHistory, renderProfile, renderTraining } from "./pages.js";
import { leaveSignals, modeOf, onTickMessage, renderSignals, tickSignals } from "./signals.js";
import { esc, toast } from "./util.js";

const view = document.getElementById("view");
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked */ } },
};

const S = {
  boot: null,
  status: null,
  signal: null,
  tab: "signals",
  wsUp: false,
  expiration: Number(store.get("expiration", 60)),
  dismissed: store.get("dismissed", null),
  busy: false,
};

const actions = {
  setExpiration(s) {
    S.expiration = s;
    store.set("expiration", s);
    haptic("light");
    view.querySelectorAll("[data-exp]").forEach((b) => b.classList.toggle("active", Number(b.dataset.exp) === s));
  },
  async getSignal(expiration) {
    if (S.busy) return;
    S.busy = true;
    haptic("medium");
    try {
      const { signal } = await api("/api/signal", { method: "POST", body: { expiration_seconds: expiration } });
      setSignal(signal);
    } catch (e) {
      haptic("error");
      toast(e.message);
      if (e instanceof ApiError && e.status === 409) refreshSignal();
    } finally {
      S.busy = false;
    }
  },
  dismiss() {
    if (S.signal) {
      S.dismissed = S.signal.id;
      store.set("dismissed", S.signal.id);
    }
    render();
  },
};

function setSignal(sig) {
  const prev = S.signal;
  S.signal = sig;
  if (sig && prev && prev.id === sig.id && prev.status !== sig.status) {
    if (sig.status === "PAIR_SELECTED") haptic("success");
    if (sig.status === "ACTIVE") haptic("heavy");
    if (sig.status === "SETTLED") haptic(sig.result === "WIN" ? "success" : sig.result === "LOSS" ? "error" : "warning");
  }
  if (S.tab === "signals") render();
}

async function refreshSignal() {
  try { setSignal((await api("/api/signal/current")).signal); } catch { /* shown via status */ }
}

function render() {
  if (S.tab === "signals") renderSignals(view, S, actions);
  else if (S.tab === "history") renderHistory(view);
  else if (S.tab === "profile") renderProfile(view, S);
  else renderTraining(view);
}

function selectTab(tab) {
  if (tab === S.tab && tab !== "signals") return;
  if (S.tab === "signals") leaveSignals();
  S.tab = tab;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  haptic("light");
  window.scrollTo(0, 0);
  render();
}

function onMessage(msg) {
  if (msg.status) S.status = msg.status;
  switch (msg.type) {
    case "hello":
    case "signal":
      setSignal(msg.signal);
      break;
    case "tick":
      onTickMessage(msg);
      break;
    case "status":
      if (S.tab === "signals") render();
      break;
    case "stats_changed":
      if (S.tab === "history") renderHistory(view);
      if (S.tab === "profile") renderProfile(view, S);
      break;
  }
}

// Fallback: if the realtime channel is down, poll so timers never get stuck.
let lastPoll = 0;
function heartbeat() {
  if (S.tab === "signals") tickSignals(view, S);
  const sig = S.signal;
  const now = Date.now();
  const waiting = sig && ["ANALYZING", "PAIR_SELECTED", "ACTIVE"].includes(sig.status);
  if (!S.wsUp && waiting && now - lastPoll > 2000) {
    lastPoll = now;
    refreshSignal();
  }
}

async function start() {
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor("#06080f");
      tg.setBackgroundColor("#06080f");
      tg.setBottomBarColor?.("#080a13");
      tg.disableVerticalSwipes?.();
    } catch { /* older clients */ }
  }
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

  try {
    S.boot = await api("/api/bootstrap");
  } catch (e) {
    view.innerHTML = `<div class="card notice bad" style="margin-top:30vh"><h2>CANNOT CONNECT</h2><p>${esc(e.message)}</p></div>`;
    return;
  }
  S.status = S.boot.status;
  S.signal = S.boot.signal;
  if (!S.boot.expirations.includes(S.expiration)) S.expiration = 60;
  render();
  connectRealtime(onMessage, (up) => {
    S.wsUp = up;
    if (S.tab === "signals" && modeOf(S) === "idle") render();
  });
  setInterval(heartbeat, 250);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshSignal(); });
}

start();
