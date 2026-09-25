"""
Append-only spend ledger.

Every routed request writes one JSON line with the tier that served it, the
model, token counts and the estimated cost. The summary endpoint folds the
file into totals per tier so the spend can be checked without a database.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config
from src.log import setup_logging

log = setup_logging("ledger")

_lock = threading.Lock()


def _ledger_path() -> Path:
    """Resolve the ledger file from Config, creating its folder if needed."""
    path = Path(Config.LEDGER_PATH)
    if not path.is_absolute():
        path = Config.PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record(
    request_id: str,
    tier: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    baseline_usd: float,
    score: int,
    fallbacks_used: int = 0,
    error: str | None = None,
) -> dict:
    """Append one request record to the ledger and return it."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id,
        "tier": tier,
        "model": model,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "cost_usd": round(float(cost_usd), 8),
        "baseline_usd": round(float(baseline_usd), 8),
        "score": int(score),
        "fallbacks_used": int(fallbacks_used),
        "error": error,
    }
    line = json.dumps(entry, separators=(",", ":"))
    path = _ledger_path()
    with _lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return entry


def read_entries(limit: int | None = None) -> list[dict]:
    """Read ledger entries, newest last. Malformed lines are skipped."""
    path = _ledger_path()
    if not path.exists():
        return []

    entries: list[dict] = []
    with _lock:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    log.warning("skipping malformed ledger line")
    if limit is not None and limit > 0:
        return entries[-limit:]
    return entries


def summarize(limit: int | None = None) -> dict:
    """Fold the ledger into totals overall and per tier."""
    entries = read_entries(limit=limit)
    by_tier: dict[str, dict] = {}
    total_cost = 0.0
    total_baseline = 0.0
    total_prompt = 0
    total_completion = 0
    errors = 0

    for entry in entries:
        tier = str(entry.get("tier", "unknown"))
        bucket = by_tier.setdefault(
            tier,
            {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
        )
        bucket["requests"] += 1
        bucket["prompt_tokens"] += int(entry.get("prompt_tokens", 0))
        bucket["completion_tokens"] += int(entry.get("completion_tokens", 0))
        bucket["cost_usd"] = round(bucket["cost_usd"] + float(entry.get("cost_usd", 0.0)), 8)
        total_cost += float(entry.get("cost_usd", 0.0))
        total_baseline += float(entry.get("baseline_usd", 0.0))
        total_prompt += int(entry.get("prompt_tokens", 0))
        total_completion += int(entry.get("completion_tokens", 0))
        if entry.get("error"):
            errors += 1

    saved = round(total_baseline - total_cost, 8)
    saved_pct = round((saved / total_baseline) * 100, 2) if total_baseline > 0 else 0.0

    return {
        "requests": len(entries),
        "errors": errors,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "cost_usd": round(total_cost, 8),
        "baseline_usd": round(total_baseline, 8),
        "saved_usd": saved,
        "saved_pct": saved_pct,
        "by_tier": by_tier,
        "ledger_path": str(_ledger_path()),
    }
