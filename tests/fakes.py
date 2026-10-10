"""TEST DOUBLES ONLY. Synthetic market data and a scripted analyzer for automated tests.

Never imported by the application. Production uses PocketOptionProvider and GeminiAnalyzer.
"""

from __future__ import annotations

import asyncio
import random
import time

from app.analysis.gemini import AIDecision, AnalysisResult, ScenarioPoint, validate_decision
from app.marketdata.base import Asset, Candle, MarketDataProvider, ProviderHealth, Quote

PAIRS = {"EURUSD_otc": 1.0850, "GBPUSD_otc": 1.2700, "AUDCHF_otc": 0.5800, "USDJPY_otc": 149.50}


class FakeProvider(MarketDataProvider):
    name = "test-fake"

    def __init__(self, tick_interval: float = 0.1, seed: int = 7):
        self.tick_interval = tick_interval
        self.rng = random.Random(seed)
        self.px = dict(PAIRS)
        self.latest: dict[str, Quote] = {}
        self.tasks: list[asyncio.Task] = []
        self.frozen = False  # set True to simulate a dead feed

    async def connect(self):
        pass

    async def disconnect(self):
        for t in self.tasks:
            t.cancel()
        self.tasks.clear()

    async def get_assets(self):
        return [Asset(s, s.replace("_otc", ""), "currency", True, True, 85 + i, (60,)) for i, s in enumerate(PAIRS)]

    async def get_candles(self, asset, interval, count):
        base = PAIRS[asset]
        last = int(time.time() // interval) * interval - interval
        out, p = [], base
        drift = base * 0.00004 * (1 if asset != "USDJPY_otc" else -1)
        for i in range(count):
            o = p
            c = o + drift + self.rng.gauss(0, base * 0.0002)
            h, lo = max(o, c) + abs(self.rng.gauss(0, base * 0.0001)), min(o, c) - abs(self.rng.gauss(0, base * 0.0001))
            out.append(Candle(asset, interval, last - (count - 1 - i) * interval, o, h, lo, c, source="provider"))
            p = c
        self.px[asset] = p
        return out

    async def subscribe_quotes(self, assets, on_quote):
        async def run():
            while True:
                await asyncio.sleep(self.tick_interval)
                if self.frozen:
                    continue
                now = time.time()
                for a in assets:
                    self.px[a] += self.rng.gauss(0, PAIRS[a] * 0.00003)
                    q = Quote(a, round(self.px[a], 5 if PAIRS[a] < 10 else 3), now - 0.05, now)
                    self.latest[a] = q
                    on_quote(q)
        self.tasks.append(asyncio.create_task(run()))

    def get_latest_quote(self, asset):
        return self.latest.get(asset)

    async def get_payout(self, asset):
        return 85

    def health(self):
        return ProviderHealth(self.name, True, True, 0, len(self.latest))


class ScriptedAnalyzer:
    """Returns a fixed decision for the top candidate. Stands in for Gemini in tests."""

    def __init__(self, decision: str = "TRADE", direction: str = "CALL", confidence: float = 0.74):
        self.model = "scripted-test"
        self.decision, self.direction, self.confidence = decision, direction, confidence
        self.state, self.error = "ready", None
        self.calls = 0

    @property
    def configured(self):
        return True

    async def check(self):
        pass

    async def analyze(self, expiration, candidates):
        self.calls += 1
        await asyncio.sleep(0.05)
        sign = 1 if self.direction == "CALL" else -1
        d = AIDecision(
            decision=self.decision, pair=candidates[0].symbol if self.decision == "TRADE" else "",
            direction=self.direction if self.decision == "TRADE" else "NONE", confidence=self.confidence,
            market_regime="bullish_momentum" if sign > 0 else "bearish_momentum",
            reason="Test decision: EMA trend and momentum aligned, volatility normal.",
            scenario=[ScenarioPoint(t=t, delta_atr=sign * v) for t, v in
                      [(0, 0), (0.25, -0.1), (0.5, 0.15), (0.75, 0.35), (1, 0.5)]],
        )
        d, dropped = validate_decision(d, {c.symbol for c in candidates})
        return AnalysisResult(d, {"test": True}, d.model_dump_json(), 50, self.model, dropped)

    def status(self):
        return {"state": self.state, "model": self.model, "error": self.error}
