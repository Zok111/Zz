"""Signal state machine. The backend is the single source of truth for state and timing.

ANALYZING -> PAIR_SELECTED -> ACTIVE -> SETTLED (WIN/LOSS/DRAW)
Terminal alternatives: NO_TRADE, CANCELLED, FAILED, VOID.

Direction is decided before PAIR_SELECTED and stored ("sealed"), but only published at
signal_at, so it cannot change with hindsight.

Price rules (deterministic, on source timestamps of recorded ticks):
- entry_price = last tick with source_ts <= entry_at, taken 1 s after entry_at
- exit_price  = last tick with source_ts <= expires_at, taken GRACE s after expires_at
- either tick older than SETTLE_MAX_QUOTE_STALENESS_SECONDS -> VOID (never guessed)
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import time
import uuid
from collections.abc import Callable

from app.analysis.gemini import PROMPT_VERSION, AIInvalid, AIUnavailable, GeminiAnalyzer, price_decimals
from app.analysis.scanner import STRATEGY_VERSION, scan
from app.config import EXPIRATIONS, Settings
from app.logs import kv
from app.marketdata.base import Quote
from app.marketdata.hub import M1, MarketHub
from app.store import TERMINAL, Store

log = logging.getLogger("signals")

ENTRY_DELAY = 1.0
SETTLE_GRACE = 2.0
SEALED_STATES = {"ANALYZING", "PAIR_SELECTED"}
SEALED_FIELDS = ("direction", "confidence", "market_regime", "reason", "scenario")
PRIVATE_FIELDS = ("ai_request", "ai_response", "candidates")


def now_ms() -> int:
    return int(time.time() * 1000)


class Busy(Exception):
    pass


class RateLimited(Exception):
    pass


class _TickTape:
    """Ticks of the selected pair, for as-of price lookups."""

    def __init__(self):
        self.ts: list[float] = []
        self.px: list[float] = []

    def add(self, q: Quote) -> None:
        if self.ts and q.source_ts < self.ts[-1]:
            i = bisect.bisect_right(self.ts, q.source_ts)
            self.ts.insert(i, q.source_ts)
            self.px.insert(i, q.price)
        else:
            self.ts.append(q.source_ts)
            self.px.append(q.price)

    def as_of(self, t: float) -> tuple[float, float] | None:
        i = bisect.bisect_right(self.ts, t)
        return (self.ts[i - 1], self.px[i - 1]) if i else None


class SignalEngine:
    def __init__(self, hub: MarketHub, analyzer: GeminiAnalyzer, store: Store, settings: Settings,
                 publish: Callable[[dict], None]):
        self.hub, self.analyzer, self.store, self.settings = hub, analyzer, store, settings
        self.publish = publish
        voided = store.void_unfinished("VOID: server restarted during the signal", now_ms())
        if voided:
            log.warning(kv("voided_unfinished", count=voided))
        self.current: dict | None = store.latest()
        self._last_request: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ public view
    def public(self, sig: dict | None) -> dict | None:
        if sig is None:
            return None
        out = {k: v for k, v in sig.items() if k not in PRIVATE_FIELDS}
        if sig.get("status") in SEALED_STATES:
            for k in SEALED_FIELDS:
                out[k] = None
        else:
            out["compared"] = [{"label": c["label"], "score": c["score"]}
                               for c in (sig.get("candidates") or {}).get("top", [])]
        return out

    def _emit(self, sig: dict) -> None:
        self.store.save(sig)
        self.publish({"type": "signal", "signal": self.public(sig)})

    # ------------------------------------------------------------------ request
    async def request(self, user_id: int, expiration: int) -> dict:
        if expiration not in EXPIRATIONS:
            raise ValueError("unsupported expiration")
        async with self._lock:
            if self.current and self.current["status"] not in TERMINAL:
                raise Busy()
            last = self._last_request.get(user_id, 0)
            if time.monotonic() - last < self.settings.signal_min_interval_seconds:
                raise RateLimited()
            self._last_request[user_id] = time.monotonic()
            sig = {
                "id": uuid.uuid4().hex, "created_at": now_ms(), "requested_by": user_id,
                "status": "ANALYZING", "stage": "scanning", "expiration_seconds": expiration,
                "candle_interval": M1, "prompt_version": PROMPT_VERSION, "strategy_version": STRATEGY_VERSION,
                "market_data_provider": self.hub.provider.name, "model_name": self.analyzer.model,
            }
            self.current = sig
            self._emit(sig)
            log.info(kv("signal_requested", id=sig["id"], user=user_id, expiration=expiration))
            self._task = asyncio.create_task(self._run(sig), name=f"signal:{sig['id'][:8]}")
            return self.public(sig)

    # ------------------------------------------------------------------ flow
    def _finish(self, sig: dict, status: str, reason: str | None = None) -> None:
        sig["status"] = status
        if reason:
            sig["fail_reason"] = reason
        sig["settled_at"] = now_ms()
        self._emit(sig)
        log.info(kv("signal_finished", id=sig["id"], status=status, result=sig.get("result"), reason=reason))
        self.publish({"type": "stats_changed"})

    async def _run(self, sig: dict) -> None:
        tape = _TickTape()
        listener = None
        try:
            if self.hub.state != "ready":
                return self._finish(sig, "FAILED", f"MARKET_DATA_UNAVAILABLE: {self.hub.error or self.hub.state}")
            result = scan(self.hub, self.settings.ai_candles)
            sig["candidates"] = {
                "top": [{"symbol": c.symbol, "label": c.label, "score": c.score, "payout": c.payout}
                        for c in result.ranked[:10]],
                "excluded": result.excluded,
            }
            if not result.ranked:
                if not any(self.hub.is_fresh(s) for s in self.hub.pairs):
                    return self._finish(sig, "FAILED", "MARKET_DATA_UNAVAILABLE: no live quotes from any pair")
                return self._finish(sig, "NO_TRADE", "No pair has enough clean recent candles yet")

            top = result.ranked[: self.settings.ai_top_candidates]
            sig["stage"] = "ai"
            sig["candidates"]["sent_to_ai"] = [c.symbol for c in top]
            self._emit(sig)

            analysis = await self.analyzer.analyze(sig["expiration_seconds"], top)
            d = analysis.decision
            sig.update({
                "ai_request": analysis.request, "ai_response": analysis.raw_text,
                "ai_latency_ms": analysis.latency_ms, "model_name": analysis.model,
                "confidence": d.confidence, "market_regime": d.market_regime, "reason": d.reason,
            })
            if d.decision == "NO_TRADE":
                return self._finish(sig, "NO_TRADE", d.reason)
            if d.confidence < self.settings.ai_min_confidence:
                return self._finish(sig, "NO_TRADE",
                                    f"Model score {d.confidence:.2f} is below the threshold "
                                    f"{self.settings.ai_min_confidence:.2f}. {d.reason}")

            chosen = next(c for c in top if c.symbol == d.pair)
            q = self.hub.latest(chosen.symbol)
            t0 = now_ms()
            countdown = self.settings.signal_countdown_seconds * 1000
            n_chart = 60 if sig["expiration_seconds"] <= 600 else 120
            sig.update({
                "status": "PAIR_SELECTED", "stage": None,
                "pair": chosen.symbol, "pair_label": chosen.label, "is_otc": int(chosen.is_otc),
                "payout": chosen.payout, "raw_score": chosen.score,
                "direction": d.direction, "scenario": [p.model_dump() for p in d.scenario],
                "atr": chosen.features["atr14"], "analysis_price": q.price if q else chosen.features["price"],
                "market_data_latency_ms": round(q.latency * 1000, 1) if q else None,
                "pair_selected_at": t0, "signal_at": t0 + countdown, "entry_at": t0 + countdown,
                "expires_at": t0 + countdown + sig["expiration_seconds"] * 1000,
                "chart": [[c.start, c.open, c.high, c.low, c.close]
                          for c in self.hub.candles(chosen.symbol, n_chart, M1, include_forming=True)],
            })

            sig["price_decimals"] = price_decimals([c[4] for c in sig["chart"]] or [sig["analysis_price"]])
            last_push = [0.0]

            def listener(quote: Quote) -> None:
                if quote.asset != sig["pair"]:
                    return
                tape.add(quote)
                if time.monotonic() - last_push[0] >= 0.5:
                    last_push[0] = time.monotonic()
                    f = self.hub.engine.forming(quote.asset, M1)
                    self.publish({"type": "tick", "signal_id": sig["id"], "price": quote.price,
                                  "ts": quote.source_ts,
                                  "candle": [f.start, f.open, f.high, f.low, f.close] if f else None})

            self.hub.add_listener(listener)
            self._emit(sig)
            log.info(kv("pair_selected", id=sig["id"], pair=sig["pair"], score=chosen.score,
                        signal_at=sig["signal_at"], expires_at=sig["expires_at"]))

            # ---- reveal
            await _sleep_until(sig["signal_at"])
            latest = self.hub.latest(sig["pair"])
            if not self.hub.is_fresh(sig["pair"]) or latest is None:
                return self._finish(sig, "CANCELLED", "Market data for the pair went stale during the countdown")
            moved = abs(latest.price - sig["analysis_price"]) / (sig["atr"] or 1e-12)
            if moved > self.settings.reveal_max_move_atr:
                return self._finish(sig, "CANCELLED",
                                    f"Price moved {moved:.1f} ATR during the countdown; setup invalidated")
            sig["status"] = "ACTIVE"
            self._emit(sig)
            log.info(kv("signal_revealed", id=sig["id"], pair=sig["pair"], direction=sig["direction"]))

            # ---- entry
            await _sleep_until(sig["entry_at"] + ENTRY_DELAY * 1000)
            entry = tape.as_of(sig["entry_at"] / 1000)
            if not self._fresh_enough(entry, sig["entry_at"]):
                return self._finish(sig, "VOID", "VOID: no recorded quote close enough to the entry time")
            sig["entry_quote_ts"], sig["entry_price"] = entry
            self._emit(sig)
            log.info(kv("entry_recorded", id=sig["id"], price=entry[1], quote_ts=entry[0]))

            # ---- settle
            await _sleep_until(sig["expires_at"] + SETTLE_GRACE * 1000)
            exit_ = tape.as_of(sig["expires_at"] / 1000)
            if not self._fresh_enough(exit_, sig["expires_at"]):
                return self._finish(sig, "VOID", "VOID: no recorded quote close enough to the expiration time")
            sig["exit_quote_ts"], sig["exit_price"] = exit_
            diff = sig["exit_price"] - sig["entry_price"]
            if diff == 0:
                sig["result"] = "DRAW"
            else:
                sig["result"] = "WIN" if (diff > 0) == (sig["direction"] == "CALL") else "LOSS"
            self._finish(sig, "SETTLED")

        except AIUnavailable as e:
            self._finish(sig, "FAILED", f"AI_UNAVAILABLE: {e}")
        except AIInvalid as e:
            self._finish(sig, "FAILED", f"AI_INVALID: {e}")
        except asyncio.CancelledError:
            self._finish(sig, "VOID", "VOID: server stopped during the signal")
            raise
        except Exception as e:
            log.exception(kv("signal_error", id=sig["id"]))
            self._finish(sig, "FAILED", f"INTERNAL_ERROR: {type(e).__name__}: {e}")
        finally:
            if listener is not None:
                self.hub.remove_listener(listener)

    def _fresh_enough(self, tick: tuple[float, float] | None, at_ms: int) -> bool:
        return tick is not None and at_ms / 1000 - tick[0] <= self.settings.settle_max_quote_staleness_seconds

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


async def _sleep_until(ts_ms: int) -> None:
    while True:
        remaining = ts_ms / 1000 - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 1.0))
