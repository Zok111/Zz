"""Builds UTC-aligned OHLC candles from a quote stream.

Deterministic rules:
- A tick belongs to bucket floor(source_ts / interval) * interval.
- Within a bucket, open/close are the earliest/latest tick by source timestamp.
- A tick older than the currently forming bucket is "late": counted and dropped.
- Missing buckets are reported as gaps; candles are never invented to fill them.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field, replace

from app.logs import kv
from app.marketdata.base import Candle, Quote

log = logging.getLogger("candles")


@dataclass
class _Series:
    closed: deque[Candle]
    forming: Candle | None = None
    first_ts: float = 0.0  # source_ts of the tick that set `open`
    last_ts: float = 0.0  # source_ts of the tick that set `close`
    late_ticks: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (first_missing_start, missing_count)


class CandleEngine:
    def __init__(self, intervals: tuple[int, ...] = (60,), max_candles: int = 300):
        self.intervals = intervals
        self.max_candles = max_candles
        self._series: dict[tuple[str, int], _Series] = {}

    def _get(self, asset: str, interval: int) -> _Series:
        key = (asset, interval)
        if key not in self._series:
            self._series[key] = _Series(closed=deque(maxlen=self.max_candles))
        return self._series[key]

    def seed(self, asset: str, interval: int, candles: list[Candle]) -> None:
        """Load closed history candles (oldest first) before live ticks arrive."""
        series = self._get(asset, interval)
        for c in sorted(candles, key=lambda c: c.start):
            if series.closed and c.start <= series.closed[-1].start:
                continue
            series.closed.append(c)

    def on_quote(self, quote: Quote) -> list[Candle]:
        """Feed one quote. Returns candles that were closed by this quote."""
        closed_now: list[Candle] = []
        for interval in self.intervals:
            done = self._apply(quote, interval)
            if done is not None:
                closed_now.append(done)
        return closed_now

    def _apply(self, q: Quote, interval: int) -> Candle | None:
        series = self._get(q.asset, interval)
        bucket = int(q.source_ts // interval) * interval
        f = series.forming

        if f is None:
            if series.closed and bucket <= series.closed[-1].start:
                series.late_ticks += 1
                return None
            self._check_gap(series, q.asset, interval, bucket)
            self._start(series, q, interval, bucket)
            return None

        if bucket == f.start:
            f.high = max(f.high, q.price)
            f.low = min(f.low, q.price)
            if q.source_ts >= series.last_ts:
                f.close, series.last_ts = q.price, q.source_ts
            if q.source_ts < series.first_ts:
                f.open, series.first_ts = q.price, q.source_ts
            f.tick_count += 1
            return None

        if bucket < f.start:
            series.late_ticks += 1
            log.debug(kv("late_tick", asset=q.asset, interval=interval, ts=q.source_ts))
            return None

        done = f
        series.closed.append(done)
        self._check_gap(series, q.asset, interval, bucket)
        self._start(series, q, interval, bucket)
        return done

    def _check_gap(self, series: _Series, asset: str, interval: int, bucket: int) -> None:
        if not series.closed:
            return
        expected = series.closed[-1].start + interval
        if bucket > expected:
            missing = (bucket - expected) // interval
            series.gaps.append((expected, missing))
            log.warning(kv("candle_gap", asset=asset, interval=interval, first_missing=expected, missing=missing))

    @staticmethod
    def _start(series: _Series, q: Quote, interval: int, bucket: int) -> None:
        series.forming = Candle(
            asset=q.asset, interval=interval, start=bucket,
            open=q.price, high=q.price, low=q.price, close=q.price, tick_count=1, source="ticks",
        )
        series.first_ts = series.last_ts = q.source_ts

    def candles(self, asset: str, interval: int, count: int, include_forming: bool = False) -> list[Candle]:
        series = self._get(asset, interval)
        out = list(series.closed)[-count:]
        if include_forming and series.forming is not None:
            out = (out + [replace(series.forming)])[-count:]
        return out

    def forming(self, asset: str, interval: int) -> Candle | None:
        f = self._get(asset, interval).forming
        return replace(f) if f else None

    def stats(self, asset: str, interval: int) -> dict:
        s = self._get(asset, interval)
        return {"closed": len(s.closed), "late_ticks": s.late_ticks, "gaps": list(s.gaps)}
