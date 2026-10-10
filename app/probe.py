"""Phase 1 market-data probe.

    python -m app.probe                       # default asset from .env, 10 minutes
    python -m app.probe --asset GBPUSD_otc --minutes 15

Connects (read-only), lists assets, loads the latest M1 history, streams live ticks,
builds candles from ticks, compares them with the provider's own candles, and writes
everything to data/phase1/<asset>_<utc-stamp>/ for inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.config import DATA_DIR, get_settings
from app.logs import kv, setup_logging
from app.marketdata.base import Candle, MarketDataError, Quote, validate_candles
from app.marketdata.candle_engine import CandleEngine
from app.marketdata.pocketoption import PocketOptionProvider

log = logging.getLogger("probe")

M1 = 60
FINE = 5  # finer internal interval, for very short expirations later


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write_candles_csv(path: Path, candles: list[Candle]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_utc", "start_epoch", "open", "high", "low", "close", "tick_count", "source"])
        for c in candles:
            w.writerow([utc(c.start), c.start, c.open, c.high, c.low, c.close, c.tick_count, c.source])


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))]


def print_header(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def compare(live: list[Candle], provider: list[Candle]) -> list[dict]:
    by_start = {c.start: c for c in provider}
    rows = []
    for c in live:
        p = by_start.get(c.start)
        if p is None:
            rows.append({"start_utc": utc(c.start), "status": "missing_in_provider"})
            continue
        diffs = {k: getattr(c, k) - getattr(p, k) for k in ("open", "high", "low", "close")}
        rows.append({
            "start_utc": utc(c.start),
            "status": "match" if all(abs(d) < 1e-9 for d in diffs.values()) else "differs",
            "ticks": c.tick_count,
            **{f"live_{k}": getattr(c, k) for k in diffs},
            **{f"po_{k}": getattr(p, k) for k in diffs},
            **{f"diff_{k}": round(d, 10) for k, d in diffs.items()},
        })
    return rows


async def run(asset: str, minutes: float, count: int) -> int:
    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = DATA_DIR / "phase1" / f"{asset}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    provider = PocketOptionProvider(settings)
    await provider.connect()
    health = provider.health()
    print(f"\nAccount type: {'DEMO' if health.is_demo else 'REAL'}  (read-only data use)")

    engine = CandleEngine(intervals=(M1, FINE), max_candles=max(300, count + 50))
    history: list[Candle] = []
    latencies: list[float] = []
    live_started_at: float | None = None
    summary: dict = {"asset": asset, "started_utc": utc(time.time())}
    ticks_file = (out / "ticks.jsonl").open("w")

    try:
        # ---------------------------------------------------------------- 1. assets
        assets = await provider.get_assets()
        (out / "assets.json").write_text(json.dumps([asdict(a) for a in assets], indent=2))
        currency = sorted((a for a in assets if a.asset_type == "currency"), key=lambda a: a.symbol)
        print_header(f"1. ASSETS  total={len(assets)}  currency={len(currency)}")
        print(f"{'symbol':<18}{'name':<24}{'otc':<6}{'active':<8}{'payout':>6}")
        for a in currency:
            print(f"{a.symbol:<18}{a.name[:23]:<24}{'yes' if a.is_otc else 'no':<6}"
                  f"{'yes' if a.is_active else 'no':<8}{str(a.payout):>6}")
        symbols = {a.symbol for a in assets}
        if asset not in symbols:
            close = [s for s in symbols if asset.split("_")[0].lower() in s.lower()]
            raise MarketDataError(f"Asset {asset!r} not found. Similar: {sorted(close)[:10]}")

        # ---------------------------------------------------------------- 2. history
        offset = await provider.ensure_time_offset(asset)
        print_header(f"2. HISTORY  {asset}  M1  (broker time offset = {offset}s"
                     f" = UTC{offset / 3600:+.1f}h, source: {provider.time_offset_source})")
        history = await provider.get_candles(asset, M1, count)
        write_candles_csv(out / "history_m1.csv", history)
        now = time.time()
        expected_last = int(now // M1) * M1 - M1
        problems = validate_candles(history, M1)
        print(f"candles received : {len(history)} (wanted {count})")
        print(f"first / last     : {utc(history[0].start)}  /  {utc(history[-1].start)} UTC")
        print(f"expected last    : {utc(expected_last)} UTC  "
              f"-> {'OK' if history[-1].start == expected_last else 'MISMATCH (%+ds)' % (history[-1].start - expected_last)}")
        print(f"validation       : {'OK' if not problems else f'{len(problems)} problem(s)'}")
        for p in problems[:15]:
            print(f"   - {p}")
        print("\nlast 10 candles (UTC):")
        for c in history[-10:]:
            print(f"   {utc(c.start)}  O={c.open}  H={c.high}  L={c.low}  C={c.close}")
        summary["history"] = {"count": len(history), "first": utc(history[0].start),
                              "last": utc(history[-1].start), "expected_last": utc(expected_last),
                              "problems": problems, "time_offset": offset}
        engine.seed(asset, M1, history)

        # ---------------------------------------------------------------- 3. live
        print_header(f"3. LIVE STREAM  {minutes:g} min  (Ctrl+C to stop early)")
        counter = {"n": 0}

        def on_quote(q: Quote) -> None:
            nonlocal live_started_at
            if live_started_at is None:
                live_started_at = q.source_ts
            counter["n"] += 1
            latencies.append(q.latency)
            ticks_file.write(json.dumps({"recv_ts": q.recv_ts, "source_ts": q.source_ts,
                                         "price": q.price, "raw": q.raw}, default=str) + "\n")
            for c in engine.on_quote(q):
                if c.interval == M1:
                    print(f"   M1 closed {utc(c.start)}  O={c.open} H={c.high} L={c.low} C={c.close}"
                          f"  ticks={c.tick_count}")

        await provider.subscribe_quotes([asset], on_quote)
        deadline = time.monotonic() + minutes * 60
        last_n = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            h = provider.health()
            q = provider.get_latest_quote(asset)
            recent = latencies[-500:]
            f = engine.forming(asset, M1)
            state = "STALE" if asset in h.stale_assets else ("OK" if h.connected else "DISCONNECTED")
            print(kv("status", state=state, ticks=counter["n"], rate=f"{(counter['n'] - last_n) / 10:.1f}/s",
                     last=q.price if q else None, age=h.last_quote_age.get(asset),
                     lat_p50=round(pct(recent, 0.5), 3), lat_p95=round(pct(recent, 0.95), 3),
                     forming=f"O={f.open} H={f.high} L={f.low} C={f.close} n={f.tick_count}" if f else None))
            last_n = counter["n"]
        return 0

    finally:
        ticks_file.close()
        # ---------------------------------------------------------------- 4. compare
        live = [c for c in engine.candles(asset, M1, count) if c.source == "ticks"]
        # The first tick-built candle started mid-minute (partial) -> not comparable.
        if live and live_started_at is not None and live[0].start <= live_started_at:
            live = live[1:]
        write_candles_csv(out / "live_m1_from_ticks.csv", live)
        write_candles_csv(out / "live_5s_from_ticks.csv", engine.candles(asset, FINE, 10_000))

        rows: list[dict] = []
        if live:
            try:
                fresh = await asyncio.wait_for(provider.get_candles(asset, M1, count), 30)
                rows = compare(live, fresh)
            except Exception as e:  # report, never hide
                print(f"\ncomparison skipped: {type(e).__name__}: {e}")
        if rows:
            with (out / "comparison_m1.csv").open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}, key=str))
                w.writeheader()
                w.writerows(rows)
        matches = sum(r["status"] == "match" for r in rows)
        print_header("4. SUMMARY")
        print(f"live ticks           : {len(latencies)}")
        if latencies:
            print(f"latency recv-source  : p50={pct(latencies, .5):.3f}s  p95={pct(latencies, .95):.3f}s  "
                  f"max={max(latencies):.3f}s  negative={sum(l < 0 for l in latencies)}"
                  f"  (negative => your PC clock is behind)")
        print(f"tick-built M1 candles: {len(live)}  (first partial candle excluded)")
        print(f"vs provider candles  : {matches}/{len(rows)} exact match")
        for r in rows:
            if r["status"] != "match":
                print(f"   {r['start_utc']}  {r['status']}  "
                      + "  ".join(f"{k}={r[k]}" for k in r if k.startswith("diff_")))
        stats = engine.stats(asset, M1)
        print(f"engine               : late_ticks={stats['late_ticks']}  gaps={stats['gaps']}")
        h = provider.health()
        print(f"provider             : parse_errors={h.parse_errors}  last_error={h.last_error}")
        summary.update({
            "finished_utc": utc(time.time()), "ticks": len(latencies),
            "latency_p50": pct(latencies, .5) if latencies else None,
            "latency_p95": pct(latencies, .95) if latencies else None,
            "live_candles": len(live), "exact_matches": matches, "compared": len(rows),
            "engine": stats, "parse_errors": h.parse_errors, "last_error": h.last_error,
        })
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        if history:
            write_chart(out / "chart.html", asset, history, live)
        print(f"\nOutput folder: {out}")
        print(f"Open the chart: {out / 'chart.html'}")
        await provider.disconnect()


def write_chart(path: Path, asset: str, history: list[Candle], live: list[Candle]) -> None:
    hist_starts = {c.start for c in history}
    data = [{"time": c.start, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "live": False}
            for c in history]
    data += [{"time": c.start, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "live": True}
             for c in live if c.start not in hist_starts]
    data.sort(key=lambda d: d["time"])
    path.write_text(CHART_TEMPLATE.replace("__ASSET__", asset).replace("__DATA__", json.dumps(data)))


CHART_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Probe __ASSET__</title>
<style>
  :root { --bg:#0b0e17; --panel:#121726; --ink:#e6e9f2; --muted:#8a93a8; --grid:#1d2335;
          --up:#22c58b; --down:#f0506e; --live-up:#5aa9ff; --live-down:#b48cff; }
  body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui, sans-serif; }
  .wrap { max-width:1200px; margin:0 auto; padding:16px; }
  h1 { font-size:18px; margin:0 0 4px; }
  .muted { color:var(--muted); }
  #chart { height:520px; background:var(--panel); border-radius:12px; margin:12px 0; }
  .legend { display:flex; gap:16px; flex-wrap:wrap; font-size:13px; }
  .sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
  #ohlc { font-variant-numeric:tabular-nums; min-height:20px; }
  table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; font-size:13px; }
  th, td { padding:4px 8px; border-bottom:1px solid var(--grid); text-align:right; }
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align:left; }
  .scroll { overflow-x:auto; }
</style></head>
<body><div class="wrap">
  <h1>__ASSET__ &middot; M1</h1>
  <div class="muted">Chart time axis is UTC. The table shows both UTC and your local time.</div>
  <div class="legend" style="margin-top:8px">
    <span><span class="sw" style="background:var(--up)"></span><span class="sw" style="background:var(--down)"></span>History (provider candles)</span>
    <span><span class="sw" style="background:var(--live-up)"></span><span class="sw" style="background:var(--live-down)"></span>Built live from ticks by CandleEngine</span>
  </div>
  <div id="chart"></div>
  <div id="ohlc" class="muted">Hover a candle for OHLC.</div>
  <h2 style="font-size:15px">Last 20 candles</h2>
  <div class="scroll"><table id="tbl"><thead><tr>
    <th>UTC</th><th>Local</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Source</th>
  </tr></thead><tbody></tbody></table></div>
</div>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script>
const DATA = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const el = document.getElementById('chart');
const chart = LightweightCharts.createChart(el, {
  autoSize: true,
  layout: { background: { color: css('--panel') }, textColor: css('--muted') },
  grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
  timeScale: { timeVisible: true, secondsVisible: false },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
});
const series = chart.addCandlestickSeries({
  upColor: css('--up'), downColor: css('--down'), wickUpColor: css('--up'), wickDownColor: css('--down'),
  borderVisible: false, priceFormat: { type: 'price', precision: 6, minMove: 0.000001 },
});
series.setData(DATA.map(d => {
  const up = d.close >= d.open;
  const c = d.live ? css(up ? '--live-up' : '--live-down') : undefined;
  return c ? { ...d, color: c, wickColor: c } : d;
}));
chart.timeScale().fitContent();
const fmtUtc = t => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19);
const fmtLocal = t => new Date(t * 1000).toLocaleString();
const byTime = new Map(DATA.map(d => [d.time, d]));
chart.subscribeCrosshairMove(p => {
  const d = p.time && byTime.get(p.time);
  document.getElementById('ohlc').textContent = d
    ? `${fmtUtc(d.time)} UTC  O ${d.open}  H ${d.high}  L ${d.low}  C ${d.close}  (${d.live ? 'live/ticks' : 'history'})`
    : 'Hover a candle for OHLC.';
});
const tb = document.querySelector('#tbl tbody');
for (const d of DATA.slice(-20).reverse()) {
  const tr = document.createElement('tr');
  for (const v of [fmtUtc(d.time), fmtLocal(d.time), d.open, d.high, d.low, d.close, d.live ? 'live/ticks' : 'history']) {
    const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
  }
  tb.appendChild(tr);
}
</script></body></html>
"""


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Phase 1 market-data probe (read-only)")
    parser.add_argument("--asset", default=settings.po_probe_asset)
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--count", type=int, default=128)
    args = parser.parse_args()
    setup_logging(settings.log_level)
    try:
        code = asyncio.run(run(args.asset, args.minutes, args.count))
    except MarketDataError as e:
        print(f"\nMARKET DATA UNAVAILABLE: {e}")
        code = 2
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
