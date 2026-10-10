"""Adapter logic against a simulated library client (no network, no real data)."""

import asyncio
import time

from app.config import Settings
from app.marketdata.pocketoption import PocketOptionProvider, _ReadOnlyClient

BROKER_OFFSET = 7200


class _Stream:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._items.pop(0)


class _FakeClient:
    def __init__(self, ticks):
        self.ticks = ticks

    async def subscribe_symbol(self, asset):
        return _Stream(self.ticks)

    async def get_candles_live(self, asset, period, hours=2.0, max_rows=100):
        # The library returns times with its hardcoded 7200 already subtracted.
        base = (int(time.time()) // 60) * 60 - 3 * 60
        yield [{"time": base + i * 60, "open": 1, "high": 2, "low": 0.5, "close": 1.5} for i in range(3)], None


def _provider(ticks, offset=None):
    p = PocketOptionProvider(Settings(_env_file=None, po_time_offset_seconds=offset))
    p._client = _ReadOnlyClient(_FakeClient(ticks))
    return p


def test_offset_autodetect_and_quote_conversion():
    now = time.time()
    ticks = [{"time": now + BROKER_OFFSET - 0.2 + i * 0.1, "price": 1.1 + i} for i in range(5)]
    p = _provider(ticks)
    got = []

    async def go():
        assert await p.ensure_time_offset("EURUSD_otc") == BROKER_OFFSET
        p._tasks["EURUSD_otc"] = asyncio.create_task(p._reader("EURUSD_otc", got.append))
        await asyncio.sleep(0.05)
        p._tasks["EURUSD_otc"].cancel()

    asyncio.run(go())
    assert len(got) == 5
    assert abs(got[0].source_ts - (now - 0.2)) < 1e-6
    assert got[-1].price == 5.1


def test_millisecond_timestamps_keep_precision():
    now = time.time()
    p = _provider([{"timestamp": (now + 3.5) * 1000, "close": 1.0}], offset=0)
    got = []

    async def go():
        task = asyncio.create_task(p._reader("A", got.append))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert abs(got[0].source_ts - (now + 3.5)) < 1e-3


def test_unparsable_tick_is_counted_not_invented():
    p = _provider([{"foo": 1}, {"time": time.time(), "price": 2.0}], offset=0)
    got = []

    async def go():
        task = asyncio.create_task(p._reader("A", got.append))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert [q.price for q in got] == [2.0]
    assert p.health().parse_errors == 1


def test_history_uses_our_offset_not_library_constant():
    p = _provider([], offset=3600)  # pretend the broker is UTC+1, not UTC+2
    candles = asyncio.run(p.get_candles("A", 60, 128))
    expected_last = (int(time.time()) // 60) * 60 - 60 + 7200 - 3600
    assert candles[-1].start == expected_last
    assert all(c.source == "provider" for c in candles)
