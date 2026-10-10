"""Gemini analyzer: compares the top candidates and returns a structured decision."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError

from app.analysis.scanner import Candidate
from app.config import Settings
from app.logs import kv

log = logging.getLogger("ai")

PROMPT_VERSION = "p1"

SYSTEM_PROMPT = """You are a cautious quantitative market analyst inside an analytics tool.
You receive several currency-pair candidates with REAL recent 1-minute candles and
precomputed technical features. A human may manually act on your analysis; you never trade.

Task: for the requested expiration, decide whether ONE candidate offers a clear setup for
price to be higher (CALL) or lower (PUT) at expiration than at entry, or that none does.

Rules:
- Use only the data provided. Do not assume news or events.
- Compare candidates against each other; pick at most one.
- NO_TRADE is a correct and expected answer when signals conflict, volatility is chaotic or
  dead, or there is no clear structure. Do not force a trade.
- "pair" must be exactly one of the candidate symbols (empty string for NO_TRADE).
- "direction": CALL, PUT, or NONE for NO_TRADE.
- "confidence": your internal 0..1 setup-quality score. It is not a win probability.
- "market_regime": a short snake_case label (e.g. bullish_momentum, range_bound, reversal_risk).
- "reason": one or two short sentences citing the features that matter. Max 220 characters.
- "scenario": 5 to 9 points describing the expected path from entry (t=0) to expiration (t=1).
  "delta_atr" is the price change from entry in units of the pair's ATR(14) on M1.
  Start with {"t":0,"delta_atr":0}. Keep it realistic for the expiration length and consistent
  with the direction. For NO_TRADE return an empty list.
Feature units: *_atr values are in ATR units; range30_pos is 0 (30-bar low) to 1 (30-bar high);
efficiency14 is 0 (noise) to 1 (perfectly directional); candle_streak is signed."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["TRADE", "NO_TRADE"]},
        "pair": {"type": "string"},
        "direction": {"type": "string", "enum": ["CALL", "PUT", "NONE"]},
        "confidence": {"type": "number"},
        "market_regime": {"type": "string"},
        "reason": {"type": "string"},
        "scenario": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "number"}, "delta_atr": {"type": "number"}},
                "required": ["t", "delta_atr"],
            },
        },
    },
    "required": ["decision", "pair", "direction", "confidence", "market_regime", "reason", "scenario"],
}


class ScenarioPoint(BaseModel):
    t: float
    delta_atr: float


class AIDecision(BaseModel):
    decision: str = Field(pattern="^(TRADE|NO_TRADE)$")
    pair: str
    direction: str = Field(pattern="^(CALL|PUT|NONE)$")
    confidence: float = Field(ge=0, le=1)
    market_regime: str
    reason: str
    scenario: list[ScenarioPoint]


class AIUnavailable(RuntimeError):
    pass


class AIInvalid(RuntimeError):
    pass


@dataclass
class AnalysisResult:
    decision: AIDecision
    request: dict
    raw_text: str
    latency_ms: int
    model: str
    scenario_dropped: bool = False


def price_decimals(values: list[float]) -> int:
    best = 0
    for v in values[-30:]:
        s = f"{v:.8f}".rstrip("0")
        best = max(best, len(s.split(".")[1]) if "." in s else 0)
    return min(best, 6)


def build_request(expiration: int, candidates: list[Candidate], n_candles: int) -> dict:
    now = datetime.now(timezone.utc)
    out = []
    for c in candidates:
        cs = c.candles[-n_candles:]
        d = price_decimals([x.close for x in cs])
        out.append({
            "pair": c.symbol, "label": c.label, "otc": c.is_otc, "payout_pct": c.payout,
            "scanner_score": c.score, "features": c.features,
            "candles_m1_format": "[minutes_ago, open, high, low, close]; last row may be the forming candle",
            "candles_m1": [[round((now.timestamp() - x.start) / 60, 1), round(x.open, d), round(x.high, d),
                            round(x.low, d), round(x.close, d)] for x in cs],
        })
    return {"expiration_seconds": expiration, "now_utc": now.isoformat(timespec="seconds"), "candidates": out}


def validate_decision(d: AIDecision, symbols: set[str]) -> tuple[AIDecision, bool]:
    """Enforce consistency. Returns (decision, scenario_dropped)."""
    if d.decision == "NO_TRADE":
        return d.model_copy(update={"pair": "", "direction": "NONE", "scenario": []}), False
    if d.pair not in symbols:
        raise AIInvalid(f"AI chose unknown pair {d.pair!r}")
    if d.direction not in ("CALL", "PUT"):
        raise AIInvalid("AI returned TRADE without CALL/PUT")
    pts = sorted((p for p in d.scenario if 0 <= p.t <= 1), key=lambda p: p.t)
    if not pts or pts[0].t > 0:
        pts.insert(0, ScenarioPoint(t=0, delta_atr=0))
    pts[0] = ScenarioPoint(t=0, delta_atr=0)
    sign = 1 if d.direction == "CALL" else -1
    dropped = len(pts) < 3 or pts[-1].t < 0.95 or pts[-1].delta_atr * sign <= 0
    return d.model_copy(update={"scenario": [] if dropped else pts}), dropped


class GeminiAnalyzer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.gemini_model
        self.state = "unconfigured"  # unconfigured | ready | error
        self.error: str | None = None
        self._client = None
        if settings.gemini_api_key and settings.gemini_model:
            from google import genai
            self._client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
            self.state = "ready"
        else:
            self.error = "GEMINI_API_KEY and GEMINI_MODEL must be set in .env"

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def check(self) -> None:
        """Verify the API key and model name at startup."""
        if not self.configured:
            return
        try:
            await asyncio.wait_for(self._client.aio.models.get(model=self.model), 20)
            self.state, self.error = "ready", None
            log.info(kv("ai_ready", model=self.model))
        except Exception as e:
            self.state, self.error = "error", f"{type(e).__name__}: {str(e)[:200]}"
            log.error(kv("ai_check_failed", model=self.model, error=self.error))

    async def analyze(self, expiration: int, candidates: list[Candidate]) -> AnalysisResult:
        if not self.configured:
            raise AIUnavailable(self.error)
        from google.genai import types

        request = build_request(expiration, candidates, self.settings.ai_candles)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
        )
        log.info(kv("ai_request", model=self.model, expiration=expiration,
                    candidates=",".join(c.symbol for c in candidates)))
        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.model, contents=json.dumps(request, separators=(",", ":")), config=config),
                self.settings.ai_timeout_seconds,
            )
        except Exception as e:
            self.state, self.error = "error", f"{type(e).__name__}: {str(e)[:200]}"
            log.error(kv("ai_error", error=self.error))
            raise AIUnavailable(self.error) from e
        latency = int((time.monotonic() - t0) * 1000)
        self.state, self.error = "ready", None
        text = resp.text or ""
        log.info(kv("ai_response", latency_ms=latency, text=text[:400].replace("\n", " ")))
        try:
            decision = AIDecision.model_validate_json(text)
        except ValidationError as e:
            raise AIInvalid(f"AI response does not match the schema: {str(e)[:200]}") from e
        decision, dropped = validate_decision(decision, {c.symbol for c in candidates})
        return AnalysisResult(decision, request, text, latency, self.model, dropped)

    def status(self) -> dict:
        return {"state": self.state, "model": self.model, "error": self.error}
