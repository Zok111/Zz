// Signals screen: expiration -> GET SIGNAL -> analyzing -> pair selected + countdown -> UP/DOWN -> result.
import { serverNow } from "./api.js";
import { SignalChart } from "./chart.js";
import { ICON, clock, esc, expLabel, price, timeOf } from "./util.js";

const TERMINAL = new Set(["SETTLED", "NO_TRADE", "CANCELLED", "FAILED", "VOID"]);
const SHOW_FINISHED_MS = 15 * 60 * 1000;
const RING = 2 * Math.PI * 74;

let mounted = { key: null, chart: null, signalId: null };

export function modeOf(S) {
  const sig = S.signal;
  if (!sig) return "idle";
  if (sig.status === "ANALYZING") return "analyzing";
  if (sig.status === "PAIR_SELECTED") return "selected";
  if (sig.status === "ACTIVE") return "active";
  if (TERMINAL.has(sig.status)) {
    if (S.dismissed === sig.id) return "idle";
    if (serverNow() - (sig.settled_at || sig.created_at) > SHOW_FINISHED_MS) return "idle";
    return sig.status === "SETTLED" ? "active" : "notice";
  }
  return "idle";
}

export function renderSignals(root, S, actions) {
  const mode = modeOf(S);
  const sig = S.signal;
  const key = [mode, sig?.id, sig?.status, mode === "analyzing" ? sig?.stage : ""].join(":");
  if (key !== mounted.key || root.dataset.view !== "signals") {
    destroyChart();
    root.dataset.view = "signals";
    root.innerHTML = `<div class="screen">${header(S)}${VIEWS[mode](S)}</div>`;
    mounted.key = key;
    bind(root, mode, S, actions);
    if (mode === "active") mountChart(root, sig);
  } else {
    patch(root, mode, S);
  }
  tickSignals(root, S);
}

export function leaveSignals() {
  destroyChart();
  mounted.key = null;
}

export function onTickMessage(msg) {
  if (mounted.chart && msg.signal_id === mounted.signalId) mounted.chart.pushCandle(msg.candle);
}

function destroyChart() {
  mounted.chart?.destroy();
  mounted.chart = null;
  mounted.signalId = null;
}

function mountChart(root, sig) {
  const el = root.querySelector("#chart");
  if (!el || !sig?.chart?.length || typeof LightweightCharts === "undefined") return;
  const keep = sig.expiration_seconds <= 300 ? 32 : sig.expiration_seconds <= 1800 ? 60 : 120;
  mounted.chart = new SignalChart(el, { ...sig, chart: sig.chart.slice(-keep) });
  mounted.signalId = sig.id;
}

// ------------------------------------------------------------------ live updates
export function tickSignals(root, S) {
  const now = serverNow();
  const sig = S.signal;
  root.querySelectorAll("[data-count-to]").forEach((el) => {
    const left = Number(el.dataset.countTo) - now;
    el.textContent = left > 0 ? Math.ceil(left / 1000) : "0";
  });
  const ring = root.querySelector(".countdown .prog");
  if (ring && sig) {
    const total = sig.signal_at - sig.pair_selected_at;
    const frac = Math.min(1, Math.max(0, (sig.signal_at - now) / total));
    ring.style.strokeDashoffset = String(RING * (1 - frac));
    const lbl = root.querySelector(".countdown .val span");
    if (lbl) lbl.textContent = sig.signal_at - now > 0 ? "Direction in" : "Revealing…";
  }
  root.querySelectorAll("[data-clock-to]").forEach((el) => {
    el.textContent = clock(Number(el.dataset.clockTo) - now);
  });
  root.querySelectorAll("[data-progress]").forEach((el) => {
    const [from, to] = el.dataset.progress.split(",").map(Number);
    el.style.width = `${Math.min(100, Math.max(0, ((now - from) / (to - from)) * 100))}%`;
  });
  const tick = root.querySelector(".ticker");
  if (tick) {
    const names = (S.status?.market?.pairs || []).filter((p) => p.live).map((p) => p.label);
    if (names.length) tick.textContent = names[Math.floor(now / 350) % names.length];
  }
}

function patch(root, mode, S) {
  const sig = S.signal;
  if (mode === "idle") {
    const card = root.querySelector("#status-card");
    if (card) card.innerHTML = statusRows(S);
    const btn = root.querySelector("#get");
    const block = blocker(S);
    if (btn) { btn.disabled = !!block; btn.textContent = block || "GET SIGNAL"; }
    root.querySelector(".live-pill").outerHTML = livePill(S);
  }
  if (mode === "active") {
    const entry = root.querySelector("#entry-price");
    if (entry) entry.textContent = price(sig.entry_price, sig.price_decimals);
    mounted.chart?.update(sig);
  }
}

// ------------------------------------------------------------------ pieces
function livePill(S) {
  const ok = S.wsUp && S.status?.market?.state === "ready";
  return `<span class="live-pill"><span class="dot ${ok ? "ok" : S.wsUp ? "warn" : "bad"}"></span>${ok ? "LIVE" : S.wsUp ? "WAIT" : "OFFLINE"}</span>`;
}

function header(S) {
  return `<header class="header">
    <div class="brand"><div class="logo">${ICON.logo}</div>
      <div><div class="brand-name">${esc(S.boot?.app_name || "")}</div><div class="brand-sub">AI MARKET ANALYSIS</div></div></div>
    ${livePill(S)}
  </header>`;
}

function blocker(S) {
  const m = S.status?.market?.state, ai = S.status?.ai?.state;
  if (!S.status) return "CONNECTING…";
  if (m === "starting") return "CONNECTING TO MARKET…";
  if (m !== "ready") return "MARKET DATA UNAVAILABLE";
  if (ai === "unconfigured") return "AI UNAVAILABLE";
  return null;
}

function statusRows(S) {
  const m = S.status?.market || {}, ai = S.status?.ai || {};
  const mRow = {
    ready: ["ok", "Market data stream connected", `${m.pairs_live}/${m.pairs_total} pairs live`],
    starting: ["warn", "Connecting to market data…", ""],
    stale: ["warn", "Market data stale", "no fresh quotes"],
    unavailable: ["bad", "MARKET DATA UNAVAILABLE", ""],
  }[m.state] || ["warn", "Waiting for server…", ""];
  const aRow = {
    ready: ["ok", "AI analyzer ready", "Gemini"],
    error: ["warn", "AI analyzer error", "will retry on request"],
    unconfigured: ["bad", "AI UNAVAILABLE", "Gemini not configured"],
  }[ai.state] || ["warn", "AI status unknown", ""];
  const row = ([cls, text, sub]) => `<div class="status-row"><span class="dot ${cls}"></span>${esc(text)}<span class="sub">${esc(sub)}</span></div>`;
  return row(mRow) + row(aRow);
}

// ------------------------------------------------------------------ views
const VIEWS = {
  idle(S) {
    const exps = S.boot?.expirations || [];
    const block = blocker(S);
    return `
      <section class="card" id="status-card">${statusRows(S)}</section>
      <section class="card">
        <div class="card-title">Expiration time</div>
        <div class="chips">${exps.map((s) => `<button class="chip ${s === S.expiration ? "active" : ""}" data-exp="${s}">${expLabel(s)}</button>`).join("")}</div>
      </section>
      <section class="card info-list">
        <div class="info">${ICON.candle}<span>Candle interval: <b>1 minute</b></span></div>
        <div class="info">${ICON.brain}<span>AI analysis uses up to the latest <b>${S.boot?.ai_candles ?? 128} candles</b></span></div>
        <div class="info">${ICON.target}<span>Currency pair: <b>selected automatically</b></span></div>
      </section>
      <button class="cta" id="get" ${block ? "disabled" : ""}>${block || "GET SIGNAL"}</button>
      <p class="footnote">Analytical signal only. Nothing is traded automatically. You decide and act manually.</p>`;
  },

  analyzing(S) {
    const st = S.signal.stage;
    const n = S.status?.market?.pairs_live ?? "";
    const step = (cls, text) => `<div class="step ${cls}"><span class="ic">${cls === "done" ? ICON.check : ""}</span>${text}</div>`;
    return `
      <section class="card scan">
        <div class="radar"><div class="ring"></div><div class="ring"></div><div class="ring"></div><div class="sweep"></div><div class="core"></div></div>
        <h2>Searching for the strongest currency pair…</h2>
        <p>${expLabel(S.signal.expiration_seconds, "caps")} expiration</p>
        <div class="ticker num"></div>
        <div class="steps">
          ${step(st === "scanning" ? "now" : "done", `Scanning ${n} live pairs`)}
          ${step(st === "scanning" ? "" : "done", "Ranking candidates by setup quality")}
          ${step(st === "ai" ? "now" : "", "AI analysis of top candidates")}
        </div>
      </section>`;
  },

  selected(S) {
    const sig = S.signal;
    return `
      <section class="card selected">
        <span class="pill">${ICON.check.replace("<svg", '<svg style="width:12px;height:12px;stroke:currentColor;stroke-width:3;fill:none"')}Currency pair selected</span>
        <div class="pair-big">${esc(baseLabel(sig))}${sig.is_otc ? '<span class="otc">OTC</span>' : ""}</div>
        <div class="meta-row"><span>Expiration <b>${expLabel(sig.expiration_seconds, "caps")}</b></span>${sig.payout ? `<span>Payout <b>${sig.payout}%</b></span>` : ""}</div>
        <div class="countdown">
          <svg viewBox="0 0 168 168">
            <defs><linearGradient id="cdGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8a6bff"/><stop offset="1" stop-color="#2f8cff"/></linearGradient></defs>
            <circle class="track" cx="84" cy="84" r="74" fill="none" stroke-width="8"/>
            <circle class="prog" cx="84" cy="84" r="74" fill="none" stroke-width="8" stroke-dasharray="${RING}" stroke-dashoffset="0"/>
          </svg>
          <div class="val"><b class="num" data-count-to="${sig.signal_at}">${S.boot?.countdown_seconds ?? ""}</b><span>Direction in</span></div>
        </div>
        <p class="instruction">Open this pair at the broker.<small>The direction will appear after the countdown.</small></p>
      </section>`;
  },

  active(S) {
    const sig = S.signal;
    const up = sig.direction === "CALL";
    const settled = sig.status === "SETTLED";
    const res = settled ? `
      <section class="result ${sig.result}">
        <b>${sig.result}</b>
        <p class="num">Entry ${price(sig.entry_price, sig.price_decimals)} → Exit ${price(sig.exit_price, sig.price_decimals)}</p>
      </section>` : "";
    return `
      ${res}
      <div class="sig-head">
        <div><div class="sig-pair">${esc(baseLabel(sig))}${sig.is_otc ? '<span class="otc">OTC</span>' : ""}</div>
          <div class="sig-sub">${expLabel(sig.expiration_seconds, "caps")} · ${sig.payout ? `payout ${sig.payout}%` : "payout n/a"}</div></div>
        <div class="timer"><b class="num" ${settled ? "" : `data-clock-to="${sig.expires_at}"`}>${settled ? "00:00" : ""}</b><span>${settled ? "Finished" : "Remaining"}</span></div>
      </div>
      <section class="direction ${up ? "up" : "down"}">
        <div><div class="word">${up ? "UP" : "DOWN"}</div><div class="label">${up ? "CALL" : "PUT"} · ${expLabel(sig.expiration_seconds, "caps")}</div></div>
        <div class="arrow">${up ? ICON.up : ICON.down}</div>
      </section>
      <div class="progress" style="margin:-4px 4px 14px"><i ${settled ? 'style="width:100%"' : `data-progress="${sig.entry_at},${sig.expires_at}"`}></i></div>
      <section class="card chart-card">
        <div class="chart-top"><span class="card-title" style="margin:0">Chart · M1</span>
          <span class="legend"><span><i class="solid"></i>Real</span><span><i></i>AI Scenario</span></span></div>
        <div class="chart" id="chart"></div>
        <p class="chart-note">Dashed line = AI forecast scenario. It is a hypothesis, not known future prices.</p>
      </section>
      <section class="card kv">
        <div><span>Entry price</span><b class="num" id="entry-price">${price(sig.entry_price, sig.price_decimals)}</b></div>
        <div><span>Entry time</span><b class="num">${timeOf(sig.entry_at)}</b></div>
        <div><span>Expires</span><b class="num">${timeOf(sig.expires_at)}</b></div>
        <div><span>Exit price</span><b class="num">${price(sig.exit_price, sig.price_decimals)}</b></div>
      </section>
      <section class="card">
        <div class="ai-head"><div class="ai-badge">AI</div>
          <div><b>AI analysis</b><small>Selected ${up ? "CALL (UP)" : "PUT (DOWN)"}</small></div></div>
        <div class="kv">
          <div><span>Model score</span><b class="num">${sig.confidence != null ? Math.round(sig.confidence * 100) + "/100" : "—"}</b></div>
          <div><span>Regime</span><b>${esc((sig.market_regime || "—").replace(/_/g, " "))}</b></div>
        </div>
        <p class="reason">${esc(sig.reason || "")}</p>
        ${sig.compared?.length ? `<div class="compared">${sig.compared.slice(0, 5).map((c) => `<span class="${c.label === sig.pair_label ? "win" : ""}">${esc(c.label)} · ${c.score}</span>`).join("")}</div>` : ""}
        <p class="chart-note">The model score is an internal rating, not a probability of winning.</p>
      </section>
      ${settled ? '<button class="cta" id="again">NEW SIGNAL</button>' : ""}`;
  },

  notice(S) {
    const sig = S.signal;
    const code = (sig.fail_reason || "").split(":")[0];
    const detail = (sig.fail_reason || "").replace(/^[A-Z_]+:\s*/, "");
    let [cls, icon, title, text] = ["warn", "∅", "NO TRADE", sig.fail_reason || "No candidate has sufficient signal quality right now."];
    if (sig.status === "FAILED" && code === "MARKET_DATA_UNAVAILABLE") [cls, icon, title, text] = ["bad", "⚠", "MARKET DATA UNAVAILABLE", detail];
    else if (sig.status === "FAILED" && code.startsWith("AI_")) [cls, icon, title, text] = ["bad", "⚠", "AI UNAVAILABLE", detail];
    else if (sig.status === "FAILED") [cls, icon, title, text] = ["bad", "⚠", "ERROR", detail];
    else if (sig.status === "CANCELLED") [cls, icon, title, text] = ["warn", "⨯", "SIGNAL CANCELLED", sig.fail_reason];
    else if (sig.status === "VOID") [cls, icon, title, text] = ["warn", "⨯", "SIGNAL VOID", detail];
    return `
      <section class="card notice ${cls}">
        <div class="big-ic">${icon}</div>
        <h2>${title}</h2>
        <p>${esc(text)}</p>
        ${sig.pair_label ? `<p style="margin-top:10px">${esc(sig.pair_label)} · ${expLabel(sig.expiration_seconds, "caps")}</p>` : ""}
      </section>
      <div class="stack">
        <button class="cta" id="retry">TRY AGAIN · ${expLabel(sig.expiration_seconds, "caps")}</button>
        <button class="cta secondary" id="back">BACK</button>
      </div>
      <p class="footnote">Refusing to trade without a clear setup is part of the strategy. Every outcome is recorded.</p>`;
  },
};

function baseLabel(sig) {
  return (sig.pair_label || sig.pair || "").replace(/\s*OTC$/i, "");
}

function bind(root, mode, S, actions) {
  root.querySelectorAll("[data-exp]").forEach((b) => b.addEventListener("click", () => actions.setExpiration(Number(b.dataset.exp))));
  root.querySelector("#get")?.addEventListener("click", () => actions.getSignal(S.expiration));
  root.querySelector("#retry")?.addEventListener("click", () => actions.getSignal(S.signal.expiration_seconds));
  root.querySelector("#back")?.addEventListener("click", () => actions.dismiss());
  root.querySelector("#again")?.addEventListener("click", () => actions.dismiss());
}

