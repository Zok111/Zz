// History, Profile and Training screens.
import { api } from "./api.js";
import { ICON, dayLabel, esc, expLabel, price, shortTime } from "./util.js";

// ------------------------------------------------------------------ History
const FILTERS = [["all", "All"], ["WIN", "Wins"], ["LOSS", "Losses"], ["DRAW", "Draws"], ["NO_TRADE", "No trade"]];
let historyFilter = "all";
let openItem = null;

function badgeFor(s) {
  if (s.status === "SETTLED") return [s.result, s.result];
  if (["ANALYZING", "PAIR_SELECTED", "ACTIVE"].includes(s.status)) return ["LIVE", "LIVE"];
  return ["OTHER", { NO_TRADE: "NO TRADE", CANCELLED: "CANCELLED", VOID: "VOID", FAILED: "FAILED" }[s.status] || s.status];
}

export async function renderHistory(root) {
  root.dataset.view = "history";
  root.innerHTML = `<div class="screen"><div class="page-title">History</div>
    <div class="filters">${FILTERS.map(([k, t]) => `<button class="filter ${k === historyFilter ? "active" : ""}" data-f="${k}">${t}</button>`).join("")}</div>
    <div id="list"><div class="empty">Loading…</div></div></div>`;
  root.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { historyFilter = b.dataset.f; renderHistory(root); }));
  let items;
  try {
    items = (await api("/api/history?limit=200")).items;
  } catch (e) {
    root.querySelector("#list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (root.dataset.view !== "history") return;
  const shown = items.filter((s) => historyFilter === "all"
    || (historyFilter === "NO_TRADE" ? s.status === "NO_TRADE" : s.status === "SETTLED" && s.result === historyFilter));
  const list = root.querySelector("#list");
  if (!shown.length) {
    list.innerHTML = `<div class="empty">No signals yet.<br>Get your first signal on the Signals tab.</div>`;
    return;
  }
  let lastDay = "";
  list.innerHTML = shown.map((s) => {
    const day = dayLabel(s.created_at);
    const head = day !== lastDay ? `<div class="day">${day}</div>` : "";
    lastDay = day;
    const [cls, text] = badgeFor(s);
    const dir = s.direction === "CALL" || s.direction === "PUT" ? s.direction : "";
    const icon = dir === "CALL" ? ICON.up : dir === "PUT" ? ICON.down : ICON.dash;
    const pair = s.pair_label ? esc(s.pair_label.replace(/\s*OTC$/i, "")) : "Market scan";
    const sub = [expLabel(s.expiration_seconds), shortTime(s.created_at), dir ? (dir === "CALL" ? "UP" : "DOWN") : ""].filter(Boolean).join(" · ");
    const details = openItem === s.id ? `<div class="details">
        ${s.entry_price != null ? `Entry <b class="num">${price(s.entry_price, s.price_decimals)}</b> → Exit <b class="num">${price(s.exit_price, s.price_decimals)}</b><br>` : ""}
        ${s.confidence != null ? `Model score ${Math.round(s.confidence * 100)}/100 · ${esc((s.market_regime || "").replace(/_/g, " "))}<br>` : ""}
        ${s.reason ? `${esc(s.reason)}<br>` : ""}
        ${s.fail_reason && s.status !== "NO_TRADE" ? `${esc(s.fail_reason)}` : ""}
      </div>` : "";
    return `${head}<div class="item" data-id="${s.id}">
        <div class="dir ${dir}">${icon}</div>
        <div class="main"><b>${pair}${s.is_otc ? '<span class="otc">OTC</span>' : ""}</b><small>${esc(sub)}</small></div>
        <span class="badge ${cls}">${text}</span>
      </div>${details}`;
  }).join("");
  list.querySelectorAll(".item").forEach((el) => el.addEventListener("click", () => {
    openItem = openItem === el.dataset.id ? null : el.dataset.id;
    renderHistory(root);
  }));
}

// ------------------------------------------------------------------ Profile
export async function renderProfile(root, S) {
  root.dataset.view = "profile";
  const u = S.boot?.user || {};
  const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || "User";
  const initials = name.split(" ").map((x) => x[0]).join("").slice(0, 2).toUpperCase();
  root.innerHTML = `<div class="screen">
    <div class="profile-head">
      <div class="avatar">${u.photo_url ? `<img src="${esc(u.photo_url)}" alt="">` : esc(initials)}</div>
      <div><b>${esc(name)}</b><small>${u.username ? "@" + esc(u.username) + " · " : ""}ID ${esc(u.id)}</small></div>
    </div>
    <div id="stats"><div class="empty">Loading…</div></div></div>`;
  let data;
  try {
    data = await api("/api/stats");
  } catch (e) {
    root.querySelector("#stats").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (root.dataset.view !== "profile") return;
  const st = data.stats;
  const streak = st.current_streak > 0 ? `+${st.current_streak}` : String(st.current_streak);
  const rate = st.win_rate == null ? "—" : `${st.win_rate.toFixed(1)}%`;
  const bar = (r) => `<div class="mini-bar"><i style="width:${r ?? 0}%"></i></div>`;
  const m = data.status.market, ai = data.status.ai;
  root.querySelector("#stats").innerHTML = `
    <section class="card">
      <div class="card-title">Statistics</div>
      <div class="stats">
        <div class="stat wide"><div><span>Win rate</span><b class="num">${rate}</b></div>
          <div style="text-align:right"><span>Total signals</span><b class="num" style="font-size:22px">${st.total}</b></div></div>
        <div class="stat up"><span>Win</span><b class="num">${st.wins}</b></div>
        <div class="stat down"><span>Loss</span><b class="num">${st.losses}</b></div>
        <div class="stat"><span>Draw</span><b class="num">${st.draws}</b></div>
        <div class="stat ${st.current_streak > 0 ? "up" : st.current_streak < 0 ? "down" : ""}"><span>Streak</span><b class="num">${streak}</b></div>
        <div class="stat up"><span>Best</span><b class="num">${st.best_streak}</b></div>
        <div class="stat ${st.worst_streak < 0 ? "down" : ""}"><span>Worst</span><b class="num">${st.worst_streak}</b></div>
      </div>
      ${st.wins + st.losses ? `<div class="rate-bar"><i style="width:${st.win_rate}%"></i></div>` : ""}
      <p class="chart-note">Calculated only from recorded signal outcomes. Draws are excluded from the win rate.
        Also recorded: ${st.no_trade} no-trade, ${st.cancelled} cancelled, ${st.void} void, ${st.failed} failed.</p>
    </section>
    ${st.by_expiration.length ? `<section class="card rows"><div class="card-title">By expiration</div>
      ${st.by_expiration.map((r) => `<div class="row"><span class="grow">${expLabel(r.expiration_seconds)}</span>
        <small class="num">${r.wins}W · ${r.losses}L</small>${bar(r.win_rate)}<b class="num" style="width:52px;text-align:right">${r.win_rate == null ? "—" : r.win_rate + "%"}</b></div>`).join("")}
    </section>` : ""}
    ${st.by_pair.length ? `<section class="card rows"><div class="card-title">By pair</div>
      ${st.by_pair.map((r) => `<div class="row"><span class="grow">${esc(r.pair)}</span>
        <small class="num">${r.wins}W · ${r.losses}L</small>${bar(r.win_rate)}<b class="num" style="width:52px;text-align:right">${r.win_rate == null ? "—" : r.win_rate + "%"}</b></div>`).join("")}
    </section>` : ""}
    <section class="card rows"><div class="card-title">System</div>
      <div class="row"><span class="dot ${m.state === "ready" ? "ok" : m.state === "starting" ? "warn" : "bad"}"></span><span class="grow">Market data</span><small>${esc(m.provider)} · ${m.pairs_live}/${m.pairs_total} live${m.is_demo ? " · demo" : ""}</small></div>
      <div class="row"><span class="dot ${ai.state === "ready" ? "ok" : ai.state === "error" ? "warn" : "bad"}"></span><span class="grow">AI analyzer</span><small>${esc(ai.model || "not configured")}</small></div>
      <div class="row"><span class="dot ${S.wsUp ? "ok" : "bad"}"></span><span class="grow">Live sync</span><small>${S.wsUp ? "connected" : "reconnecting"}</small></div>
    </section>
    <p class="footnote">Signals are analytical only. The app never places trades or accesses your broker balance.</p>`;
}

// ------------------------------------------------------------------ Training
export function renderTraining(root) {
  root.dataset.view = "training";
  const lesson = (n, t, p) => `<div class="lesson"><div class="n">${n}</div><div><b>${t}</b><p>${p}</p></div></div>`;
  root.innerHTML = `<div class="screen">
    <div class="page-title">Training</div>
    <section class="card">
      <div class="card-title">How a signal works</div>
      ${lesson(1, "Choose the expiration", "Pick how long the analysis horizon is, from 30 seconds to 4 hours. You do not choose a pair.")}
      ${lesson(2, "Press GET SIGNAL", "The server scans every live currency pair, scores each one from real 1-minute candles and sends the strongest few to the Gemini AI.")}
      ${lesson(3, "Open the selected pair", "The app shows the chosen pair first. Use the countdown to open the same pair and expiration at your broker.")}
      ${lesson(4, "Direction is revealed", "When the countdown ends you see UP (CALL) or DOWN (PUT), the chart and the AI scenario. The decision was fixed before the reveal.")}
      ${lesson(5, "Decide yourself", "The app never trades. If you act, you place the trade manually.")}
      ${lesson(6, "Automatic result", "At expiration the server compares the exit price with the entry price from the recorded quote stream and saves WIN, LOSS or DRAW.")}
    </section>
    <section class="card">
      <div class="card-title">Reading the screen</div>
      <div class="term"><b><span class="up-t">UP · CALL</span></b>The analysis expects the price at expiration to be above the entry price.</div>
      <div class="term"><b><span class="down-t">DOWN · PUT</span></b>The analysis expects the price at expiration to be below the entry price.</div>
      <div class="term"><b><span class="dash"></span>AI Scenario</b>The dashed line is the model's hypothetical path. It is not real or known future data.</div>
      <div class="term"><b>Model score</b>An internal 0–100 rating of setup quality. It is not a probability of winning. Only the recorded statistics show real performance.</div>
      <div class="term"><b>NO TRADE</b>When no pair has a clear setup, the AI refuses to pick one. This is intended and protects you from weak signals.</div>
      <div class="term"><b>OTC</b>Over-the-counter quotes generated by the broker, available when regular markets are closed. They can behave differently from regular pairs.</div>
    </section>
    <section class="card">
      <div class="card-title">Good practice</div>
      <div class="term">Test on a demo account first and judge the system only by a large number of recorded signals.</div>
      <div class="term">Your broker's entry happens a few seconds after ours, so results can differ, especially at 30 s – 1 min.</div>
      <div class="term">Never risk money you cannot afford to lose. Past results do not guarantee future results.</div>
    </section>
  </div>`;
}
