"""Provider-independent market-data models and the MarketDataProvider interface.

All timestamps are UTC epoch seconds (float for quotes, int for candle starts).
The rest of the application depends only on this module, never on a specific provider.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Asset:
    symbol: str  # provider symbol, e.g. "EURUSD_otc"
    name: str  # human-readable, e.g. "EUR/USD OTC"
    asset_type: str  # "currency", "stock", "commodity", "cryptocurrency", "index", ...
    is_otc: bool
    is_active: bool
    payout: int | None  # percent, as reported by the source
    allowed_candles: tuple[int, ...] = ()


@dataclass(frozen=True)
class Quote:
    asset: str
    price: float
    source_ts: float  # UTC epoch seconds, from the source (after offset correction)
    recv_ts: float  # UTC epoch seconds, local clock when we received it
    raw: Any = field(default=None, compare=False, repr=False)

    @property
    def latency(self) -> float:
        return self.recv_ts - self.source_ts


@dataclass
class Candle:
    asset: str
    interval: int  # seconds
    start: int  # UTC epoch seconds, aligned to `interval`
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    tick_count: int = 0
    source: str = "ticks"  # "ticks" (built by CandleEngine) or "provider" (history from source)


@dataclass
class ProviderHealth:
    provider: str
    connected: bool
    is_demo: bool | None = None
    time_offset_seconds: int | None = None
    quotes_received: int = 0
    parse_errors: int = 0
    last_quote_age: dict[str, float] = field(default_factory=dict)  # asset -> seconds
    stale_assets: list[str] = field(default_factory=list)
    last_error: str | None = None


QuoteCallback = Callable[[Quote], None]


class MarketDataError(RuntimeError):
    """Market data is unavailable or invalid. Never replaced by fabricated data."""


class MarketDataProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def get_assets(self) -> list[Asset]: ...

    @abc.abstractmethod
    async def subscribe_quotes(self, assets: list[str], on_quote: QuoteCallback) -> None:
        """Start streaming quotes for `assets`; `on_quote` is called for every quote."""

    @abc.abstractmethod
    def get_latest_quote(self, asset: str) -> Quote | None: ...

    @abc.abstractmethod
    async def get_candles(self, asset: str, interval: int, count: int) -> list[Candle]:
        """Return up to `count` most recent CLOSED candles, oldest first."""

    @abc.abstractmethod
    async def get_payout(self, asset: str) -> int | None: ...

    @abc.abstractmethod
    def health(self) -> ProviderHealth: ...


def validate_candles(candles: list[Candle], interval: int) -> list[str]:
    """Return human-readable problems: misalignment, ordering, gaps, OHLC violations."""
    problems: list[str] = []
    for i, c in enumerate(candles):
        if c.start % interval:
            problems.append(f"#{i} start={c.start} not aligned to {interval}s")
        if not (c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) and c.low <= c.high):
            problems.append(f"#{i} start={c.start} OHLC invariant violated")
        if i:
            step = c.start - candles[i - 1].start
            if step <= 0:
                problems.append(f"#{i} start={c.start} not strictly increasing")
            elif step % interval:
                problems.append(f"#{i} start={c.start} irregular step {step}s")
            elif step != interval:
                missing = step // interval - 1
                problems.append(f"gap before start={c.start}: {missing} missing candle(s)")
    return problems
