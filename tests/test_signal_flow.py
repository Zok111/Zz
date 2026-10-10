"""End-to-end signal lifecycle on test doubles: state order, sealing, timestamps, settlement."""

import asyncio
import time

import pytest

import app.signals as signals_mod
from app.analysis.gemini import AIDecision, ScenarioPoint, validate_decision, AIInvalid
from app.config import Settings
from app.marketdata.hub import MarketHub
from app.signals import Busy, SignalEngine
from app.store import Store, compute_stats
from tests.fakes import FakeProvider, ScriptedAnalyzer


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(signals_mod, "EXPIRATIONS", [1, 30, 60])
    monkeypatch.setattr(signals_mod, "ENTRY_DELAY", 0.2)
    monkeypatch.setattr(signals_mod, "SETTLE_GRACE", 0.3)


def make(tmp_path, analyzer, provider=None, **over):
    s = Settings(_env_file=None, signal_countdown_seconds=1, signal_min_interval_seconds=0,
                 db_path=tmp_path / "t.db", **over)
    events: list[dict] = []
    hub = MarketHub(provider or FakeProvider(), s)
    store = Store(s.db_path)
    return hub, store, SignalEngine(hub, analyzer, store, s, events.append), events


async def boot(hub):
    hub.start()
    for _ in range(100):
        if hub.state == "ready" and all(hub.is_fresh(p) for p in hub.pairs):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"hub not ready: {hub.state} {hub.error}")


def test_full_trade_lifecycle(tmp_path, fast):
    hub, store, engine, events = make(tmp_path, ScriptedAnalyzer())

    async def go():
        await boot(hub)
        await engine.request(42, 1)
        with pytest.raises(Busy):
            await engine.request(42, 1)
        await asyncio.wait_for(engine._task, 10)
        await hub.stop()

    asyncio.run(go())
    seq = [e["signal"]["status"] for e in events if e["type"] == "signal"]
    assert seq[0] == "ANALYZING" and "PAIR_SELECTED" in seq and "ACTIVE" in seq and seq[-1] == "SETTLED"
    for e in events:
        if e["type"] == "signal" and e["signal"]["status"] in ("ANALYZING", "PAIR_SELECTED"):
            assert e["signal"]["direction"] is None and e["signal"]["scenario"] is None  # sealed
    sig = store.latest()
    assert sig["result"] in ("WIN", "LOSS", "DRAW")
    assert sig["entry_at"] == sig["signal_at"] == sig["pair_selected_at"] + 1000
    assert sig["expires_at"] == sig["entry_at"] + 1000
    assert sig["entry_quote_ts"] <= sig["entry_at"] / 1000 and sig["exit_quote_ts"] <= sig["expires_at"] / 1000
    expected = ("WIN" if sig["exit_price"] > sig["entry_price"] else "LOSS") if sig["exit_price"] != sig["entry_price"] else "DRAW"
    assert sig["result"] == expected  # CALL
    assert sig["chart"] and sig["candidates"]["top"]
    stats = compute_stats(store.settled(), store.status_counts())
    assert stats["total"] == 1


def test_no_trade_is_recorded(tmp_path, fast):
    hub, store, engine, _ = make(tmp_path, ScriptedAnalyzer(decision="NO_TRADE"))

    async def go():
        await boot(hub)
        await engine.request(1, 30)
        await asyncio.wait_for(engine._task, 5)
        await hub.stop()

    asyncio.run(go())
    assert store.latest()["status"] == "NO_TRADE"


def test_low_confidence_becomes_no_trade(tmp_path, fast):
    hub, store, engine, _ = make(tmp_path, ScriptedAnalyzer(confidence=0.3))

    async def go():
        await boot(hub)
        await engine.request(1, 30)
        await asyncio.wait_for(engine._task, 5)
        await hub.stop()

    asyncio.run(go())
    assert store.latest()["status"] == "NO_TRADE"
    assert "below the threshold" in store.latest()["fail_reason"]


def test_dead_feed_fails_honestly(tmp_path, fast):
    prov = FakeProvider()
    hub, store, engine, _ = make(tmp_path, ScriptedAnalyzer(), provider=prov, po_stale_after_seconds=0.3)

    async def go():
        await boot(hub)
        prov.frozen = True
        await asyncio.sleep(0.5)
        await engine.request(1, 30)
        await asyncio.wait_for(engine._task, 5)
        await hub.stop()

    asyncio.run(go())
    sig = store.latest()
    assert sig["status"] == "FAILED" and sig["fail_reason"].startswith("MARKET_DATA_UNAVAILABLE")


def test_feed_dies_during_countdown_cancels(tmp_path, fast):
    prov = FakeProvider()
    hub, store, engine, _ = make(tmp_path, ScriptedAnalyzer(), provider=prov, po_stale_after_seconds=0.4)

    async def go():
        await boot(hub)
        await engine.request(1, 30)
        await asyncio.sleep(0.3)
        prov.frozen = True
        await asyncio.wait_for(engine._task, 5)
        await hub.stop()

    asyncio.run(go())
    assert store.latest()["status"] == "CANCELLED"


def test_unfinished_signals_are_voided_on_restart(tmp_path):
    store = Store(tmp_path / "r.db")
    store.save({"id": "x", "created_at": 1, "status": "ACTIVE"})
    s = Settings(_env_file=None, db_path=tmp_path / "r.db")
    SignalEngine(MarketHub(FakeProvider(), s), ScriptedAnalyzer(), store, s, lambda m: None)
    assert store.get("x")["status"] == "VOID"


def test_validate_decision_rules():
    base = dict(decision="TRADE", pair="A", direction="CALL", confidence=0.7, market_regime="x", reason="r",
                scenario=[ScenarioPoint(t=0, delta_atr=0), ScenarioPoint(t=0.5, delta_atr=0.2), ScenarioPoint(t=1, delta_atr=0.4)])
    d, dropped = validate_decision(AIDecision(**base), {"A"})
    assert not dropped and len(d.scenario) == 3
    d, dropped = validate_decision(AIDecision(**{**base, "direction": "PUT"}), {"A"})
    assert dropped and d.scenario == []  # scenario contradicts direction -> not drawn
    with pytest.raises(AIInvalid):
        validate_decision(AIDecision(**{**base, "pair": "B"}), {"A"})
