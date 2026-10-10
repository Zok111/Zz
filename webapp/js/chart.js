// Candlestick chart with real candles plus a visually distinct, dashed "AI Scenario" path.
/* global LightweightCharts */

const C = {
  up: "#19d38c", down: "#ff4b6e", grid: "rgba(255,255,255,0.05)", text: "#7d86a3",
  scenario: "#9d8bff", entry: "#c9cfdf", exit: "#ffb547",
};

const localTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const localTimeSec = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function decimalsOf(values) {
  let d = 0;
  for (const v of values.slice(-30)) {
    const s = String(v);
    if (s.includes(".")) d = Math.max(d, s.split(".")[1].length);
  }
  return Math.min(Math.max(d, 2), 6);
}

export class SignalChart {
  constructor(el, signal) {
    this.signal = signal;
    const prices = (signal.chart || []).map((c) => c[4]);
    const precision = signal.price_decimals ?? decimalsOf(prices);
    this.chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: C.text, fontSize: 10, attributionLogo: true },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.15, bottom: 0.12 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 2,
                   tickMarkFormatter: (t) => localTime(t) },
      localization: { timeFormatter: (t) => localTimeSec(t) },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      handleScroll: false, handleScale: false,
    });
    const priceFormat = { type: "price", precision, minMove: 1 / 10 ** precision };
    this.candles = this.chart.addCandlestickSeries({
      upColor: C.up, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down, borderVisible: false,
      priceFormat, lastValueVisible: true, priceLineVisible: false,
    });
    this.scenario = this.chart.addLineSeries({
      color: C.scenario, lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed,
      lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
      priceFormat, title: "AI Scenario",
    });
    this.lastStart = 0;
    this.setCandles(signal.chart || []);
    this.update(signal);
  }

  setCandles(rows) {
    const data = rows.map(([t, o, h, l, c]) => ({ time: t, open: o, high: h, low: l, close: c }));
    this.candles.setData(data);
    this.lastStart = data.length ? data[data.length - 1].time : 0;
  }

  // Live forming candle from the server: [start, o, h, l, c]
  pushCandle(row) {
    if (!row || row[0] < this.lastStart) return;
    const [t, o, h, l, c] = row;
    this.candles.update({ time: t, open: o, high: h, low: l, close: c });
    this.lastStart = t;
  }

  update(signal) {
    this.signal = signal;
    const ref = signal.entry_price ?? signal.analysis_price;
    const pts = signal.scenario || [];
    const entryT = Math.floor(signal.entry_at / 1000);
    const expT = Math.floor(signal.expires_at / 1000);

    // AI scenario: control points (t in 0..1, delta in ATR units) -> smooth dashed path.
    if (pts.length >= 2 && ref && signal.atr) {
      const dense = smooth(pts, 40);
      const seen = new Set();
      const line = [];
      for (const p of dense) {
        const time = Math.round(entryT + p.t * (expT - entryT));
        if (seen.has(time)) continue;
        seen.add(time);
        line.push({ time, value: ref + p.delta_atr * signal.atr });
      }
      this.scenario.setData(line);
      this.scenario.setMarkers([{ time: line[line.length - 1].time, position: "inBar", color: C.scenario, shape: "circle" }]);
    } else {
      this.scenario.setData([]);
    }

    if (this.entryLine) this.candles.removePriceLine(this.entryLine);
    if (this.exitLine) this.candles.removePriceLine(this.exitLine);
    this.entryLine = this.exitLine = null;
    if (signal.entry_price != null) {
      this.entryLine = this.candles.createPriceLine({ price: signal.entry_price, color: C.entry, lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: "Entry" });
    }
    if (signal.exit_price != null) {
      this.exitLine = this.candles.createPriceLine({ price: signal.exit_price, color: C.exit, lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: "Exit" });
    }
    const entryBar = Math.floor(entryT / 60) * 60;
    if (signal.status === "ACTIVE" || signal.status === "SETTLED") {
      const up = signal.direction === "CALL";
      this.candles.setMarkers(entryBar <= this.lastStart ? [{
        time: entryBar, position: up ? "belowBar" : "aboveBar", color: up ? C.up : C.down,
        shape: up ? "arrowUp" : "arrowDown", text: up ? "CALL" : "PUT",
      }] : []);
    }
    this.chart.timeScale().fitContent();
  }

  destroy() {
    this.chart.remove();
  }
}

// Monotone-ish smoothing between control points (Catmull-Rom on delta, linear in t).
function smooth(points, steps) {
  const p = [...points].sort((a, b) => a.t - b.t);
  const out = [];
  for (let i = 0; i < p.length - 1; i++) {
    const p0 = p[Math.max(0, i - 1)], p1 = p[i], p2 = p[i + 1], p3 = p[Math.min(p.length - 1, i + 2)];
    const n = Math.max(2, Math.round(steps / (p.length - 1)));
    for (let k = 0; k < n; k++) {
      const s = k / n;
      const s2 = s * s, s3 = s2 * s;
      const d = 0.5 * ((2 * p1.delta_atr) + (-p0.delta_atr + p2.delta_atr) * s
        + (2 * p0.delta_atr - 5 * p1.delta_atr + 4 * p2.delta_atr - p3.delta_atr) * s2
        + (-p0.delta_atr + 3 * p1.delta_atr - 3 * p2.delta_atr + p3.delta_atr) * s3);
      out.push({ t: p1.t + (p2.t - p1.t) * s, delta_atr: d });
    }
  }
  out.push(p[p.length - 1]);
  return out;
}
