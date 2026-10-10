export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function expLabel(s, style = "chip") {
  if (s < 60) return style === "chip" ? `${s} sec` : `${s} SEC`;
  if (s < 3600) return style === "chip" ? `${s / 60} min` : `${s / 60} MIN`;
  return style === "chip" ? `${s / 3600} h` : `${s / 3600} H`;
}

export function clock(ms) {
  const t = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export const timeOf = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
export const shortTime = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function dayLabel(ms) {
  const d = new Date(ms), today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
}

export const price = (v, d) => (v == null ? "—" : d != null ? Number(v).toFixed(d) : String(v));

export const ICON = {
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/></svg>',
  dash: '<svg viewBox="0 0 24 24"><path d="M6 12h12"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  candle: '<svg viewBox="0 0 24 24"><path d="M7 3v4M7 17v4M17 3v6M17 19v2"/><rect x="4.5" y="7" width="5" height="10" rx="1"/><rect x="14.5" y="9" width="5" height="10" rx="1"/></svg>',
  brain: '<svg viewBox="0 0 24 24"><path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V6a2 2 0 0 0-3-2z"/><path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/></svg>',
  target: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8"/></svg>',
  shield: '<svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg>',
  logo: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 3 3.5 19h17z" stroke="#fff" stroke-width="2" stroke-linejoin="round"/><path d="M12 3v16M12 11l8.5 8" stroke="#fff" stroke-opacity=".6" stroke-width="1.6"/></svg>',
};

let toastTimer;
export function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}
