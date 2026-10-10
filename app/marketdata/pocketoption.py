"""Pocket Option market-data provider (READ-ONLY).

STATUS: UNOFFICIAL. Pocket Option publishes no public API. This adapter uses the
community library `BinaryOptionsToolsV2` (reverse-engineered WebSocket client), which
authenticates with a browser session string (SSID). It may break without notice, and
using it may conflict with the broker's terms of service.

READ-ONLY GUARANTEE: the library also contains trading functions. This module only
ever talks to the library through `_ReadOnlyClient`, which exposes a fixed whitelist of
market-data methods and raises on anything else. tests/test_readonly_guard.py checks it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time
from typing import Any

from app.config import Settings
from app.logs import kv
from app.marketdata.base import (
    Asset,
    Candle,
    MarketDataError,
    MarketDataProvider,
    ProviderHealth,
    Quote,
    QuoteCallback,
)

log = logging.getLogger("marketdata.pocketoption")

# Offset the library itself hardcodes in get_candles_live() (pocketoption/asynchronous.py,
# `platform_time_offset = 7200`): it subtracts this from every raw timestamp. We undo it
# and apply our own detected/configured offset instead, so history and live ticks agree.
_LIB_HISTORY_OFFSET = 7200

_OFFSET_SAMPLE_TICKS = 5
_OFFSET_ROUNDING = 1800  # broker offsets are whole or half hours
_MAX_CLOCK_RESIDUAL = 5.0  # seconds; larger means local clock skew or heavy latency


class _ReadOnlyClient:
    """Whitelist proxy around PocketOptionAsync. Trading methods are unreachable."""

    ALLOWED = frozenset({
        "wait_for_assets",
        "is_demo",
        "is_connected",
        "active_assets",
        "payout",
        "get_candles_live",
        "subscribe_symbol",
        "get_server_time",
        "shutdown",
    })

    def __init__(self, client: Any):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str):
        if name not in self.ALLOWED:
            raise PermissionError(f"'{name}' is not a read-only market-data method")
        return getattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name, value):
        raise PermissionError("read-only client")


def normalize_ssid(ssid: str) -> str:
    """Same prefix normalization the library applies (fixes shell-stripped quotes)."""
    return re.sub(r"""42\[['"]?auth['"]?\s*,""", '42["auth",', ssid.strip(), count=1)


def check_ssid_format(ssid: str) -> bool:
    """Check the SSID shape without ever logging its content."""
    if not ssid.startswith('42["auth",'):
        return False
    try:
        payload = json.loads(ssid[2:])
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, list) and len(payload) >= 2 and isinstance(payload[1], dict)
        and "session" in payload[1] and "uid" in payload[1]
    )


def _raw_ts(raw: dict) -> float:
    value = float(raw.get("timestamp", raw.get("time")))
    return value / 1000.0 if value > 1e11 else value  # ms -> s, keep sub-second precision


def _raw_price(raw: dict) -> float:
    return float(raw.get("price", raw.get("close")))


class PocketOptionProvider(MarketDataProvider):
    name = "pocketoption"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: _ReadOnlyClient | None = None
        self._is_demo: bool | None = None
        self._offset: int | None = settings.po_time_offset_seconds
        self._offset_source = "config" if self._offset is not None else "auto"
        self._clock_residual: float | None = None
        self._latest: dict[str, Quote] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._quotes_received = 0
        self._parse_errors = 0
        self._last_error: str | None = None

    # ----------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._settings.po_ssid is None:
            raise MarketDataError("PO_SSID is empty. Add it to .env (see README).")
        ssid = normalize_ssid(self._settings.po_ssid.get_secret_value())
        if not check_ssid_format(ssid):
            raise MarketDataError(
                'PO_SSID has the wrong format. It must look like 42["auth",{"session":"...","uid":...}]'
            )

        from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

        log.info(kv("connecting", provider=self.name))
        t0 = time.monotonic()
        client = _ReadOnlyClient(PocketOptionAsync(ssid))
        try:
            await client.wait_for_assets(timeout=60.0)
        except Exception as e:
            await self._safe_shutdown(client)
            self._last_error = f"connect: {type(e).__name__}"
            raise MarketDataError(f"Could not connect / load assets: {e}") from e

        self._is_demo = bool(client.is_demo())
        if self._settings.po_require_demo and not self._is_demo:
            await self._safe_shutdown(client)
            raise MarketDataError(
                "This SSID belongs to a REAL account. Use a demo-account SSID, "
                "or set PO_REQUIRE_DEMO=false if you knowingly accept the risk."
            )
        self._client = client
        log.info(kv("connected", provider=self.name, demo=self._is_demo,
                    seconds=round(time.monotonic() - t0, 2)))

    async def disconnect(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._client is not None:
            await self._safe_shutdown(self._client)
            self._client = None
        log.info(kv("disconnected", provider=self.name))

    @staticmethod
    async def _safe_shutdown(client: _ReadOnlyClient) -> None:
        try:
            await client.shutdown()
        except Exception as e:  # shutting down must never mask the original error
            log.warning(kv("shutdown_error", error=type(e).__name__))

    def _require_client(self) -> _ReadOnlyClient:
        if self._client is None:
            raise MarketDataError("Not connected")
        return self._client

    # ----------------------------------------------------------------- assets
    async def get_assets(self) -> list[Asset]:
        raw_assets = await self._require_client().active_assets()
        assets = []
        for a in raw_assets:
            allowed = []
            for x in a.get("allowed_candles") or []:
                try:
                    allowed.append(int(x if not isinstance(x, dict) else x.get("time")))
                except (TypeError, ValueError):
                    pass
            assets.append(Asset(
                symbol=str(a.get("symbol")),
                name=str(a.get("name") or a.get("symbol")),
                asset_type=str(a.get("asset_type") or "unknown").lower(),
                is_otc=bool(a.get("is_otc")),
                is_active=bool(a.get("is_active", True)),
                payout=a.get("payout"),
                allowed_candles=tuple(allowed),
            ))
        log.info(kv("assets_loaded", total=len(assets),
                    currency=sum(1 for a in assets if a.asset_type == "currency")))
        return assets

    async def get_payout(self, asset: str) -> int | None:
        return await self._require_client().payout(asset)

    # ----------------------------------------------------------------- time offset
    @property
    def time_offset(self) -> int | None:
        return self._offset

    @property
    def time_offset_source(self) -> str:
        return self._offset_source

    async def ensure_time_offset(self, asset: str, timeout: float = 20.0) -> int:
        """Detect the broker timestamp offset from live ticks (unless configured)."""
        if self._offset is not None:
            return self._offset
        client = self._require_client()
        stream = await client.subscribe_symbol(asset)
        diffs: list[float] = []
        unparsed: list[str] = []

        async def sample():
            async for raw in stream:
                try:
                    diffs.append(_raw_ts(raw) - time.time())
                except (TypeError, ValueError, AttributeError):
                    unparsed.append(str(raw)[:200])
                    continue
                if len(diffs) >= _OFFSET_SAMPLE_TICKS:
                    return

        try:
            await asyncio.wait_for(sample(), timeout)
        except asyncio.TimeoutError:
            pass
        if not diffs:
            detail = f" ({len(unparsed)} unparsable messages, first: {unparsed[0]})" if unparsed else ""
            raise MarketDataError(
                f"No usable ticks for {asset} within {timeout}s; cannot detect time offset. "
                f"Is the asset open right now?{detail}"
            )
        diff = statistics.median(diffs)
        self._offset = int(round(diff / _OFFSET_ROUNDING) * _OFFSET_ROUNDING)
        self._clock_residual = diff - self._offset
        log.info(kv("time_offset_detected", asset=asset, offset=self._offset,
                    residual=round(self._clock_residual, 3), samples=len(diffs)))
        if abs(self._clock_residual) > _MAX_CLOCK_RESIDUAL:
            log.warning(kv("clock_skew", residual=round(self._clock_residual, 3),
                           hint="sync your PC clock (w32tm /resync)"))
        return self._offset

    # ----------------------------------------------------------------- candles
    async def get_candles(self, asset: str, interval: int, count: int) -> list[Candle]:
        client = self._require_client()
        offset = await self.ensure_time_offset(asset)
        hours = max(0.1, (count + 5) * interval / 3600.0)
        gen = client.get_candles_live(asset, interval, hours=hours, max_rows=count)
        try:
            closed, _forming = await gen.__anext__()
        except StopAsyncIteration:
            closed = []
        finally:
            await gen.aclose()

        candles = []
        for c in closed:
            start = int(c["time"]) + _LIB_HISTORY_OFFSET - offset
            candles.append(Candle(
                asset=asset, interval=interval, start=start,
                open=float(c["open"]), high=float(c["high"]),
                low=float(c["low"]), close=float(c["close"]),
                volume=c.get("volume"), source="provider",
            ))
        if not candles:
            raise MarketDataError(f"No candle history returned for {asset}")
        log.info(kv("history_loaded", asset=asset, interval=interval, candles=len(candles),
                    first=candles[0].start, last=candles[-1].start))
        return candles[-count:]

    # ----------------------------------------------------------------- live quotes
    async def subscribe_quotes(self, assets: list[str], on_quote: QuoteCallback) -> None:
        for asset in assets:
            if asset not in self._tasks:
                await self.ensure_time_offset(asset)
                self._tasks[asset] = asyncio.create_task(self._reader(asset, on_quote), name=f"po:{asset}")

    async def _reader(self, asset: str, on_quote: QuoteCallback) -> None:
        backoff = 1.0
        while True:
            try:
                stream = await self._require_client().subscribe_symbol(asset)
                log.info(kv("subscribed", asset=asset))
                async for raw in stream:
                    recv = time.time()
                    try:
                        quote = Quote(asset=asset, price=_raw_price(raw),
                                      source_ts=_raw_ts(raw) - self._offset, recv_ts=recv, raw=raw)
                    except (TypeError, ValueError, AttributeError):
                        self._parse_errors += 1
                        if self._parse_errors <= 5:
                            log.warning(kv("tick_parse_error", asset=asset, raw=str(raw)[:200]))
                        continue
                    backoff = 1.0
                    self._quotes_received += 1
                    self._latest[asset] = quote
                    on_quote(quote)
                log.warning(kv("stream_ended", asset=asset))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"{asset}: {type(e).__name__}: {e}"
                log.warning(kv("stream_error", asset=asset, error=type(e).__name__, detail=str(e)[:200]))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def get_latest_quote(self, asset: str) -> Quote | None:
        return self._latest.get(asset)

    # ----------------------------------------------------------------- health
    def health(self) -> ProviderHealth:
        now = time.time()
        ages = {a: round(now - q.recv_ts, 3) for a, q in self._latest.items()}
        stale = [a for a in self._tasks if ages.get(a, float("inf")) > self._settings.po_stale_after_seconds]
        connected = False
        if self._client is not None:
            try:
                connected = bool(self._client.is_connected())
            except Exception:
                connected = False
        return ProviderHealth(
            provider=self.name, connected=connected, is_demo=self._is_demo,
            time_offset_seconds=self._offset, quotes_received=self._quotes_received,
            parse_errors=self._parse_errors, last_quote_age=ages, stale_assets=stale,
            last_error=self._last_error,
        )
