from app.marketdata.base import Candle, Quote, validate_candles
from app.marketdata.candle_engine import CandleEngine

T0 = 1_760_000_040  # aligned to 60s


def q(ts: float, price: float, asset: str = "EURUSD_otc") -> Quote:
    return Quote(asset=asset, price=price, source_ts=ts, recv_ts=ts + 0.1)


def test_builds_ohlc_and_closes_on_next_bucket():
    e = CandleEngine(intervals=(60,))
    for ts, p in [(T0 + 1, 1.10), (T0 + 20, 1.15), (T0 + 30, 1.05), (T0 + 59.9, 1.12)]:
        assert e.on_quote(q(ts, p)) == []
    closed = e.on_quote(q(T0 + 60, 1.13))
    assert len(closed) == 1
    c = closed[0]
    assert (c.start, c.open, c.high, c.low, c.close, c.tick_count) == (T0, 1.10, 1.15, 1.05, 1.12, 4)
    assert e.forming("EURUSD_otc", 60).start == T0 + 60


def test_bucket_boundary_is_inclusive_start():
    e = CandleEngine(intervals=(60,))
    e.on_quote(q(T0 + 59.999, 1.0))
    closed = e.on_quote(q(T0 + 60.0, 2.0))
    assert closed[0].close == 1.0
    assert e.forming("EURUSD_otc", 60).open == 2.0


def test_out_of_order_tick_within_bucket_does_not_override_close_or_open():
    e = CandleEngine(intervals=(60,))
    e.on_quote(q(T0 + 10, 1.0))
    e.on_quote(q(T0 + 30, 1.2))
    e.on_quote(q(T0 + 20, 0.9))  # arrives late, but same bucket
    f = e.forming("EURUSD_otc", 60)
    assert (f.open, f.high, f.low, f.close) == (1.0, 1.2, 0.9, 1.2)
    e.on_quote(q(T0 + 5, 0.95))  # earlier than the first tick -> becomes open
    assert e.forming("EURUSD_otc", 60).open == 0.95


def test_late_tick_from_closed_bucket_is_dropped_and_counted():
    e = CandleEngine(intervals=(60,))
    e.on_quote(q(T0 + 10, 1.0))
    e.on_quote(q(T0 + 70, 1.1))
    e.on_quote(q(T0 + 50, 9.9))
    assert e.candles("EURUSD_otc", 60, 10)[0].high == 1.0
    assert e.stats("EURUSD_otc", 60)["late_ticks"] == 1


def test_gap_is_reported_not_filled():
    e = CandleEngine(intervals=(60,))
    e.on_quote(q(T0 + 1, 1.0))
    e.on_quote(q(T0 + 61, 1.0))
    e.on_quote(q(T0 + 241, 1.0))  # skips T0+120 and T0+180
    closed = e.candles("EURUSD_otc", 60, 10)
    assert [c.start for c in closed] == [T0, T0 + 60]
    assert e.stats("EURUSD_otc", 60)["gaps"] == [(T0 + 120, 2)]


def test_seed_then_live_continues_and_detects_gap_after_history():
    e = CandleEngine(intervals=(60,))
    hist = [Candle("EURUSD_otc", 60, T0 + i * 60, 1, 1, 1, 1, source="provider") for i in range(3)]
    e.seed("EURUSD_otc", 60, hist)
    e.on_quote(q(T0 + 180 + 5, 1.0))  # directly after history: no gap
    assert e.stats("EURUSD_otc", 60)["gaps"] == []
    e.on_quote(q(T0 + 120, 5.0))  # older than history -> late
    assert e.stats("EURUSD_otc", 60)["late_ticks"] == 1


def test_multiple_intervals_and_assets_are_independent():
    e = CandleEngine(intervals=(60, 5))
    e.on_quote(q(T0 + 1, 1.0))
    e.on_quote(q(T0 + 1, 7.0, asset="GBPUSD_otc"))
    closed = e.on_quote(q(T0 + 6, 1.1))
    assert [c.interval for c in closed] == [5]
    assert e.forming("GBPUSD_otc", 60).close == 7.0


def test_validate_candles():
    good = [Candle("A", 60, T0 + i * 60, 1, 2, 0.5, 1.5) for i in range(3)]
    assert validate_candles(good, 60) == []
    bad = [Candle("A", 60, T0, 1, 2, 0.5, 1.5), Candle("A", 60, T0 + 180, 1, 0.9, 0.5, 1.5),
           Candle("A", 60, T0 + 181, 1, 2, 0.5, 1.5)]
    problems = validate_candles(bad, 60)
    assert any("gap" in p and "2 missing" in p for p in problems)
    assert any("OHLC" in p for p in problems)
    assert any("not aligned" in p for p in problems)
