"""Scans every live pair, computes features and ranks them deterministically."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.analysis.features import MIN_CANDLES, clarity_score, compute_features
from app.logs import kv
from app.marketdata.base import Candle
from app.marketdata.hub import FINE, M1, MarketHub

log = logging.getLogger("scanner")

STRATEGY_VERSION = "scan-v1"


@dataclass
class Candidate:
    symbol: str
    label: str
    is_otc: bool
    payout: int | None
    score: float
    features: dict
    candles: list[Candle] = field(repr=False)


@dataclass
class ScanResult:
    ranked: list[Candidate]
    excluded: dict[str, str]  # symbol -> reason
    scanned: int


def scan(hub: MarketHub, candles_n: int) -> ScanResult:
    ranked: list[Candidate] = []
    excluded: dict[str, str] = {}
    for symbol in hub.pairs:
        asset = hub.assets.get(symbol)
        if asset is None or not asset.is_active:
            excluded[symbol] = "inactive"
            continue
        if not hub.is_fresh(symbol):
            excluded[symbol] = "stale"
            continue
        candles = hub.candles(symbol, candles_n, M1, include_forming=True)
        if len(candles) < MIN_CANDLES:
            excluded[symbol] = f"only {len(candles)} candles"
            continue
        if hub.recent_gaps(symbol):
            excluded[symbol] = "gaps in recent candles"
            continue
        fine = hub.candles(symbol, 12, FINE, include_forming=True)
        features = compute_features(candles, fine)
        ranked.append(Candidate(symbol, hub.label(symbol), asset.is_otc, asset.payout,
                                clarity_score(features), features, candles))
    ranked.sort(key=lambda c: (-c.score, -(c.payout or 0), c.symbol))
    log.info(kv("scan", scanned=len(hub.pairs), ranked=len(ranked), excluded=len(excluded),
                top=",".join(f"{c.symbol}:{c.score}" for c in ranked[:5])))
    return ScanResult(ranked, excluded, len(hub.pairs))
