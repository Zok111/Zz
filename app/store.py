"""SQLite persistence for signals. Every signal, including NO_TRADE and failures, is kept."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "created_at": "INTEGER",  # all *_at are UTC epoch milliseconds
    "requested_by": "INTEGER",
    "status": "TEXT",
    "stage": "TEXT",
    "pair": "TEXT",
    "pair_label": "TEXT",
    "is_otc": "INTEGER",
    "payout": "INTEGER",
    "expiration_seconds": "INTEGER",
    "candle_interval": "INTEGER",
    "pair_selected_at": "INTEGER",
    "signal_at": "INTEGER",
    "entry_at": "INTEGER",
    "expires_at": "INTEGER",
    "settled_at": "INTEGER",
    "direction": "TEXT",
    "confidence": "REAL",
    "market_regime": "TEXT",
    "reason": "TEXT",
    "scenario": "TEXT",  # json
    "atr": "REAL",
    "price_decimals": "INTEGER",
    "analysis_price": "REAL",
    "entry_price": "REAL",
    "entry_quote_ts": "REAL",
    "exit_price": "REAL",
    "exit_quote_ts": "REAL",
    "result": "TEXT",
    "fail_reason": "TEXT",
    "model_name": "TEXT",
    "prompt_version": "TEXT",
    "strategy_version": "TEXT",
    "raw_score": "REAL",
    "candidates": "TEXT",  # json: ranked candidates + exclusions
    "ai_request": "TEXT",  # json: exact payload sent to the model (market snapshot)
    "ai_response": "TEXT",  # raw model text
    "chart": "TEXT",  # json: candles shown with the signal
    "market_data_provider": "TEXT",
    "market_data_latency_ms": "REAL",
    "ai_latency_ms": "INTEGER",
}
JSON_COLUMNS = {"scenario", "candidates", "ai_request", "chart"}
TERMINAL = {"SETTLED", "NO_TRADE", "CANCELLED", "FAILED", "VOID"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            cols = ", ".join(f"{k} {v}" for k, v in COLUMNS.items())
            self._db.execute(f"CREATE TABLE IF NOT EXISTS signals ({cols})")
            existing = {r[1] for r in self._db.execute("PRAGMA table_info(signals)")}
            for k, v in COLUMNS.items():
                if k not in existing:
                    self._db.execute(f"ALTER TABLE signals ADD COLUMN {k} {v.replace('PRIMARY KEY', '')}")
            self._db.execute("CREATE INDEX IF NOT EXISTS ix_signals_created ON signals(created_at)")
            self._db.commit()

    @staticmethod
    def _encode(sig: dict) -> dict:
        return {k: (json.dumps(sig.get(k)) if k in JSON_COLUMNS and sig.get(k) is not None else sig.get(k))
                for k in COLUMNS}

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in JSON_COLUMNS:
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def save(self, sig: dict) -> None:
        enc = self._encode(sig)
        cols = ", ".join(enc)
        marks = ", ".join(f":{k}" for k in enc)
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO signals ({cols}) VALUES ({marks})", enc)
            self._db.commit()

    def get(self, signal_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return self._decode(row) if row else None

    def latest(self) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._decode(row) if row else None

    def history(self, limit: int = 50, before: int | None = None) -> list[dict]:
        light = [k for k in COLUMNS if k not in ("ai_request", "ai_response", "candidates", "chart")]
        q = f"SELECT {', '.join(light)} FROM signals"
        args: list = []
        if before:
            q += " WHERE created_at < ?"
            args.append(before)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._decode(r) for r in rows]

    def void_unfinished(self, reason: str, now_ms: int) -> int:
        marks = ",".join("?" * len(TERMINAL))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE signals SET status='VOID', fail_reason=?, settled_at=? WHERE status NOT IN ({marks})",
                (reason, now_ms, *TERMINAL))
            self._db.commit()
            return cur.rowcount

    def settled(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT pair, pair_label, is_otc, expiration_seconds, direction, result, confidence, expires_at "
                "FROM signals WHERE status='SETTLED' ORDER BY expires_at").fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM signals GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}


def compute_stats(settled: list[dict], counts: dict[str, int]) -> dict:
    """All numbers come from recorded outcomes. Draws neither extend nor break streaks."""
    def summarize(rows: list[dict]) -> dict:
        w = sum(r["result"] == "WIN" for r in rows)
        l = sum(r["result"] == "LOSS" for r in rows)
        d = sum(r["result"] == "DRAW" for r in rows)
        return {"total": len(rows), "wins": w, "losses": l, "draws": d,
                "win_rate": round(w / (w + l) * 100, 1) if w + l else None}

    cur = best = worst = 0
    for r in settled:
        if r["result"] == "WIN":
            cur = cur + 1 if cur > 0 else 1
        elif r["result"] == "LOSS":
            cur = cur - 1 if cur < 0 else -1
        best, worst = max(best, cur), min(worst, cur)

    by_exp: dict[int, list] = {}
    by_pair: dict[str, list] = {}
    for r in settled:
        by_exp.setdefault(r["expiration_seconds"], []).append(r)
        by_pair.setdefault(r["pair_label"] or r["pair"], []).append(r)

    return {
        **summarize(settled),
        "current_streak": cur,
        "best_streak": best,
        "worst_streak": worst,
        "no_trade": counts.get("NO_TRADE", 0),
        "cancelled": counts.get("CANCELLED", 0),
        "failed": counts.get("FAILED", 0),
        "void": counts.get("VOID", 0),
        "requests": sum(counts.values()),
        "by_expiration": [{"expiration_seconds": k, **summarize(v)} for k, v in sorted(by_exp.items())],
        "by_pair": sorted(({"pair": k, **summarize(v)} for k, v in by_pair.items()),
                          key=lambda x: -x["total"])[:10],
    }
