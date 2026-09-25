"""
Cost-aware routing policy for chat completion requests.

Scores each request on a few cheap signals (prompt length, code or math
content, tool definitions, requested output size) and picks the cheapest
model tier that can handle it. Tiers are ordered from cheapest to most
expensive; the fallback order walks up the ladder when a provider errors.

Nothing here talks to a provider. The route module asks for a decision,
tries the tiers in order, and hands the result to the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.log import setup_logging

log = setup_logging("router_policy")

# Price per 1M tokens, in USD. Made up but in the right ballpark for
# small, mid and large models behind an OpenAI-compatible endpoint.
PRICE_TABLE: dict[str, dict[str, float]] = {
    "small": {"input": 0.15, "output": 0.60},
    "medium": {"input": 1.10, "output": 4.40},
    "large": {"input": 5.00, "output": 15.00},
}

TIER_ORDER: tuple[str, ...] = ("small", "medium", "large")

# Model id per tier. The gateway exposes these as its own model names so a
# client can pin a tier explicitly instead of letting the router decide.
TIER_MODELS: dict[str, str] = {
    "small": "catgpt-small",
    "medium": "catgpt-medium",
    "large": "catgpt-browser",
}

MODEL_TO_TIER: dict[str, str] = {model: tier for tier, model in TIER_MODELS.items()}

_CODE_HINTS = re.compile(
    r"(```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b|\bSELECT\b|\bpublic static\b|\bpackage\b)",
    re.IGNORECASE,
)
_MATH_HINTS = re.compile(r"(\bprove\b|\bderive\b|\bintegral\b|\btheorem\b|\bstep by step\b)", re.IGNORECASE)

# Score thresholds. A request scoring at or above a threshold needs that tier.
MEDIUM_THRESHOLD = 3
LARGE_THRESHOLD = 7


@dataclass(frozen=True)
class RouteDecision:
    """What the router decided for one request."""

    tier: str
    model: str
    score: int
    reasons: list[str] = field(default_factory=list)
    fallback: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "model": self.model,
            "score": self.score,
            "reasons": list(self.reasons),
            "fallback": list(self.fallback),
        }


def _message_text(messages: list[dict]) -> str:
    """Flatten message content into one string for scoring."""
    parts: list[str] = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


def score_request(
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
) -> tuple[int, list[str]]:
    """
    Score a chat completion request from 0 upward.

    Each signal adds a small amount; the reasons list explains the score so
    the ledger and the summary endpoint can show why a tier was chosen.
    """
    text = _message_text(messages)
    score = 0
    reasons: list[str] = []

    length = len(text)
    if length > 12000:
        score += 4
        reasons.append("very long prompt")
    elif length > 4000:
        score += 2
        reasons.append("long prompt")
    elif length < 400:
        reasons.append("short prompt")

    if _CODE_HINTS.search(text):
        score += 2
        reasons.append("code in prompt")

    if _MATH_HINTS.search(text):
        score += 3
        reasons.append("reasoning request")

    if tools:
        score += 2
        reasons.append(f"{len(tools)} tool definitions")

    if max_tokens is not None and max_tokens > 2000:
        score += 2
        reasons.append("large output budget")

    return score, reasons


def tier_for_score(score: int) -> str:
    """Map a score to the cheapest tier that can handle it."""
    if score >= LARGE_THRESHOLD:
        return "large"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    return "small"


def fallback_order(tier: str, enabled: bool = True) -> list[str]:
    """
    Tiers to try after the chosen one, cheapest first.

    With fallback disabled the list is empty and a provider error is final.
    """
    if not enabled:
        return []
    index = TIER_ORDER.index(tier)
    return list(TIER_ORDER[index + 1 :])


def decide(
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    requested_model: str | None = None,
    default_tier: str = "small",
    fallback_enabled: bool = True,
) -> RouteDecision:
    """
    Pick a tier for one request.

    A client that names one of the tier models pins that tier; anything else
    (including the gateway's own default model name) is scored.
    """
    if requested_model in MODEL_TO_TIER:
        tier = MODEL_TO_TIER[requested_model]
        score, reasons = score_request(messages, tools, max_tokens)
        reasons = [f"pinned to {tier}"] + reasons
    else:
        score, reasons = score_request(messages, tools, max_tokens)
        tier = tier_for_score(score)
        if tier == "small" and default_tier in TIER_ORDER and default_tier != "small":
            tier = default_tier
            reasons.append(f"default tier {default_tier}")

    decision = RouteDecision(
        tier=tier,
        model=TIER_MODELS[tier],
        score=score,
        reasons=reasons,
        fallback=fallback_order(tier, fallback_enabled),
    )
    log.debug(f"route decision: {decision.as_dict()}")
    return decision


def estimate_cost(tier: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate the USD cost of one call from its token counts."""
    prices = PRICE_TABLE.get(tier)
    if prices is None:
        return 0.0
    cost = (prompt_tokens / 1_000_000) * prices["input"]
    cost += (completion_tokens / 1_000_000) * prices["output"]
    return round(cost, 8)


def baseline_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """What the same call would have cost on the most expensive tier."""
    return estimate_cost("large", prompt_tokens, completion_tokens)
