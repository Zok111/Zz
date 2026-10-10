"""Deterministic technical features computed from real candles.

Price-distance features are expressed in ATR(14) units so pairs with very different
price scales (EUR/USD vs USD/JPY) are comparable.
"""

from __future__ import annotations

from app.marketdata.base import Candle

MIN_CANDLES = 60


def ema_series(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], n: int = 14) -> float:
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def true_ranges(c: list[Candle]) -> list[float]:
    out = [c[0].high - c[0].low]
    for i in range(1, len(c)):
        out.append(max(c[i].high - c[i].low, abs(c[i].high - c[i - 1].close), abs(c[i].low - c[i - 1].close)))
    return out


def atr(c: list[Candle], n: int = 14) -> float:
    tr = true_ranges(c)
    a = sum(tr[1 : n + 1]) / n
    for v in tr[n + 1 :]:
        a = (a * (n - 1) + v) / n
    return a


def efficiency_ratio(closes: list[float], n: int = 14) -> float:
    """Kaufman: net move / path length over n bars. 1 = perfectly directional, 0 = noise."""
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes)))
    return abs(closes[-1] - closes[-1 - n]) / path if path else 0.0


def compute_features(candles: list[Candle], fine: list[Candle] | None = None) -> dict:
    """`candles`: M1, oldest first, last one may be forming. Needs >= MIN_CANDLES."""
    if len(candles) < MIN_CANDLES:
        raise ValueError(f"need {MIN_CANDLES} candles, got {len(candles)}")
    closes = [c.close for c in candles]
    last = candles[-1]
    a = atr(candles) or 1e-12
    tr = true_ranges(candles)
    tr60 = sum(tr[-60:]) / 60 or 1e-12
    e9, e21, e50 = ema_series(closes, 9), ema_series(closes, 21), ema_series(closes, 50)

    def ret(n: int) -> float:
        return (closes[-1] - closes[-1 - n]) / a

    streak = 0
    for c in reversed(candles):
        d = 1 if c.close > c.open else -1 if c.close < c.open else 0
        if d == 0 or (streak and (d > 0) != (streak > 0)):
            break
        streak += d

    w30 = candles[-30:]
    hi30, lo30 = max(c.high for c in w30), min(c.low for c in w30)
    prior20 = candles[-21:-1]
    hi20, lo20 = max(c.high for c in prior20), min(c.low for c in prior20)
    ema_trend = (e9[-1] - e21[-1]) / a

    f = {
        "price": last.close,
        "atr14": a,
        "atr_pct": a / last.close * 100 if last.close else 0.0,
        "vol_regime": a / tr60,
        "ema9": e9[-1], "ema21": e21[-1], "ema50": e50[-1],
        "ema_trend_atr": ema_trend,
        "ema9_slope_atr": (e9[-1] - e9[-6]) / a,
        "price_vs_ema50_atr": (last.close - e50[-1]) / a,
        "rsi14": rsi(closes),
        "efficiency14": efficiency_ratio(closes, 14),
        "ret1_atr": ret(1), "ret3_atr": ret(3), "ret5_atr": ret(5), "ret15_atr": ret(15),
        "accel_atr": ret(3) - (closes[-4] - closes[-7]) / a,
        "candle_streak": streak,
        "range30_pos": (last.close - lo30) / (hi30 - lo30) if hi30 > lo30 else 0.5,
        "dist_high30_atr": (hi30 - last.close) / a,
        "dist_low30_atr": (last.close - lo30) / a,
        "breakout20": "up" if last.close > hi20 else "down" if last.close < lo20 else "none",
        "last3": [
            {"body_atr": (c.close - c.open) / a, "upper_wick_atr": (c.high - max(c.open, c.close)) / a,
             "lower_wick_atr": (min(c.open, c.close) - c.low) / a}
            for c in candles[-3:]
        ],
        "trend": "up" if ema_trend > 0.15 else "down" if ema_trend < -0.15 else "flat",
    }
    up = f["trend"] == "up"
    down = f["trend"] == "down"
    last2_down = candles[-1].close < candles[-1].open and candles[-2].close < candles[-2].open
    last2_up = candles[-1].close > candles[-1].open and candles[-2].close > candles[-2].open
    f["pullback"] = ("in_uptrend" if up and last2_down else "in_downtrend" if down and last2_up else "none")
    if fine and len(fine) >= 7:
        f["fine5s_ret6_atr"] = (fine[-1].close - fine[-7].close) / a
        f["fine5s_streak"] = sum(1 if c.close > c.open else -1 if c.close < c.open else 0 for c in fine[-6:])
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in f.items()}


def clarity_score(f: dict) -> float:
    """Deterministic 0-100 ranking of how clear/structured the setup is (not a win probability)."""
    direction = 1 if f["ema_trend_atr"] > 0 else -1
    agree = sum(1 for k in ("ema9_slope_atr", "ret5_atr", "ret15_atr") if f[k] * direction > 0) / 3
    trend = min(abs(f["ema_trend_atr"]) / 1.5, 1.0) * agree
    vr = f["vol_regime"]
    vol = 1.0 if 0.6 <= vr <= 1.8 else max(0.0, 1 - abs(vr - (0.6 if vr < 0.6 else 1.8)) / 0.6)
    rsi_room = 1.0 if (direction > 0 and f["rsi14"] < 72) or (direction < 0 and f["rsi14"] > 28) else 0.0
    score = 35 * f["efficiency14"] + 30 * trend + 20 * vol + 15 * rsi_room
    return round(max(0.0, min(100.0, score)), 1)
