"""MarketHub: one live connection, all scanned pairs, candles kept warm in memory."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from app.config import Settings
from app.logs import kv
from app.marketdata.base import Asset, Candle, MarketDataError, MarketDataProvider, Quote, validate_candles
from app.marketdata.candle_engine import CandleEngine

log = logging.getLogger("hub")

M1 = 60
FINE = 5
HISTORY_CANDLES = 150
RETRY_SECONDS = 30
ASSET_REFRESH_SECONDS = 300


def pair_label(asset: Asset | None, symbol: str) -> str:
    """'EURUSD_otc' -> 'EUR/USD OTC' (uses the source's name when it already looks right)."""
    if asset and "/" in asset.name:
        return asset.name
    base = symbol.split("_")[0].upper()
    label = f"{base[:3]}/{base[3:]}" if len(base) == 6 else base
    return f"{label} OTC" if symbol.lower().endswith("_otc") else label


class MarketHub:
    def __init__(self, provider: MarketDataProvider, settings: Settings):
        self.provider = provider
        self.settings = settings
        self.engine = CandleEngine(intervals=(M1, FINE), max_candles=400)
        self.assets: dict[str, Asset] = {}
        self.pairs: list[str] = []
        self.state = "starting"  # starting | ready | unavailable
        self.error: str | None = None
        self._listeners: list[Callable[[Quote], None]] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="market-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        try:
            await self.provider.disconnect()
        except Exception:
            pass

    async def _supervise(self) -> None:
        while True:
            try:
                await self._boot()
                while True:
                    await asyncio.sleep(ASSET_REFRESH_SECONDS)
                    await self._refresh_assets()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state, self.error = "unavailable", str(e)
                log.error(kv("market_unavailable", error=type(e).__name__, detail=str(e)[:300]))
                try:
                    await self.provider.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(RETRY_SECONDS)

    async def _boot(self) -> None:
        self.state, self.error = "starting", None
        await self.provider.connect()
        await self._refresh_assets()
        self.pairs = self._choose_pairs()
        if not self.pairs:
            raise MarketDataError("No active currency pairs match the scanner settings right now")
        log.info(kv("pairs_selected", count=len(self.pairs), pairs=",".join(self.pairs)))

        sem = asyncio.Semaphore(3)

        async def load(symbol: str) -> None:
            async with sem:
                try:
                    candles = await asyncio.wait_for(self.provider.get_candles(symbol, M1, HISTORY_CANDLES), 40)
                    self.engine.seed(symbol, M1, candles)
                except Exception as e:  # pair can still warm up from live ticks
                    log.warning(kv("history_failed", asset=symbol, error=type(e).__name__, detail=str(e)[:200]))

        await asyncio.gather(*(load(s) for s in self.pairs))
        await self.provider.subscribe_quotes(self.pairs, self._on_quote)
        self.state = "ready"
        log.info(kv("market_ready", pairs=len(self.pairs)))

    async def _refresh_assets(self) -> None:
        self.assets = {a.symbol: a for a in await self.provider.get_assets()}

    def _choose_pairs(self) -> list[str]:
        s = self.settings
        ok = [
            a for a in self.assets.values()
            if a.asset_type == "currency" and a.is_active
            and (a.payout or 0) >= s.scan_min_payout
            and ((a.is_otc and s.scan_include_otc) or (not a.is_otc and s.scan_include_regular))
        ]
        ok.sort(key=lambda a: (-(a.payout or 0), a.symbol))
        return [a.symbol for a in ok[: s.scan_max_pairs]]

    # ------------------------------------------------------------------ quotes
    def add_listener(self, fn: Callable[[Quote], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[Quote], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _on_quote(self, q: Quote) -> None:
        self.engine.on_quote(q)
        for fn in list(self._listeners):
            try:
                fn(q)
            except Exception as e:
                log.warning(kv("listener_error", error=type(e).__name__, detail=str(e)[:200]))

    # ------------------------------------------------------------------ queries
    def latest(self, symbol: str) -> Quote | None:
        return self.provider.get_latest_quote(symbol)

    def is_fresh(self, symbol: str) -> bool:
        q = self.latest(symbol)
        return q is not None and time.time() - q.recv_ts <= self.settings.po_stale_after_seconds

    def candles(self, symbol: str, n: int, interval: int = M1, include_forming: bool = True) -> list[Candle]:
        return self.engine.candles(symbol, interval, n, include_forming=include_forming)

    def recent_gaps(self, symbol: str, n: int = 30) -> list[str]:
        return [p for p in validate_candles(self.candles(symbol, n, include_forming=False), M1) if "gap" in p]

    def label(self, symbol: str) -> str:
        return pair_label(self.assets.get(symbol), symbol)

    def status(self) -> dict:
        fresh = [s for s in self.pairs if self.is_fresh(s)] if self.state == "ready" else []
        h = self.provider.health()
        return {
            "state": self.state if not (self.state == "ready" and not fresh) else "stale",
            "error": self.error,
            "provider": self.provider.name,
            "is_demo": h.is_demo,
            "pairs_total": len(self.pairs),
            "pairs_live": len(fresh),
            "pairs": [{"symbol": s, "label": self.label(s), "live": s in fresh} for s in self.pairs],
        }
