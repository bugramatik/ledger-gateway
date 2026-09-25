"""
Routing routes — cost-aware chat completions and the spend summary.

Endpoints:
  POST /v1/route/chat/completions   Score the request, try tiers in order, log spend
  GET  /v1/route/ledger/summary     Totals per tier from the JSONL ledger
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException

from src.api import ledger, router_policy
from src.api.openai_schemas import ChatCompletionRequest, ChatCompletionResponse
from src.api.routes import get_client
from src.config import Config
from src.log import setup_logging

log = setup_logging("routing_routes")

router = APIRouter(prefix="/v1/route", tags=["routing"])


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: about four characters per token."""
    return max(1, len(text) // 4)


def _prompt_tokens(request: ChatCompletionRequest) -> int:
    total = 0
    for message in request.messages:
        content = message.content
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    total += _estimate_tokens(text)
    return total


def _messages_as_dicts(request: ChatCompletionRequest) -> list[dict]:
    return [message.model_dump(exclude_none=True) for message in request.messages]


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def routed_chat_completion(request: ChatCompletionRequest) -> ChatCompletionResponse:
    """Serve a chat completion through the cheapest tier that fits."""
    client = get_client()
    request_id = f"route-{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()

    messages = _messages_as_dicts(request)
    tools = [tool.model_dump(exclude_none=True) for tool in (request.tools or [])]
    decision = router_policy.decide(
        messages=messages,
        tools=tools,
        max_tokens=request.max_tokens,
        requested_model=request.model,
        default_tier=Config.ROUTER_DEFAULT_TIER,
        fallback_enabled=Config.ROUTER_FALLBACK_ENABLED,
    )

    tiers = [decision.tier] + decision.fallback
    last_error: Exception | None = None
    result = None
    served_tier = decision.tier
    fallbacks_used = 0

    for index, tier in enumerate(tiers):
        model = router_policy.TIER_MODELS[tier]
        try:
            result = await client.send_message(
                _render_prompt(messages),
                model=model,
            )
            served_tier = tier
            fallbacks_used = index
            break
        except Exception as exc:  # provider error, page error, timeout
            last_error = exc
            log.warning(f"tier {tier} failed for {request_id}: {exc}")

    if result is None:
        ledger.record(
            request_id=request_id,
            tier=decision.tier,
            model=decision.model,
            prompt_tokens=_prompt_tokens(request),
            completion_tokens=0,
            cost_usd=0.0,
            baseline_usd=0.0,
            score=decision.score,
            fallbacks_used=len(tiers) - 1,
            error=str(last_error) if last_error else "no tier served the request",
        )
        raise HTTPException(status_code=502, detail=f"All tiers failed: {last_error}")

    prompt_tokens = _prompt_tokens(request)
    completion_tokens = _estimate_tokens(result.message or "")
    cost = router_policy.estimate_cost(served_tier, prompt_tokens, completion_tokens)
    baseline = router_policy.baseline_cost(prompt_tokens, completion_tokens)

    ledger.record(
        request_id=request_id,
        tier=served_tier,
        model=router_policy.TIER_MODELS[served_tier],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        baseline_usd=baseline,
        score=decision.score,
        fallbacks_used=fallbacks_used,
    )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        f"{request_id} served by {served_tier} in {elapsed_ms}ms "
        f"(score {decision.score}, {fallbacks_used} fallbacks)"
    )

    return ChatCompletionResponse(
        id=request_id,
        created=int(time.time()),
        model=router_policy.TIER_MODELS[served_tier],
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.message or ""},
                "finish_reason": "stop",
            }
        ],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


@router.get("/ledger/summary")
async def ledger_summary(limit: int | None = None) -> dict:
    """Return spend totals overall and per tier."""
    return ledger.summarize(limit=limit)


def _render_prompt(messages: list[dict]) -> str:
    """Flatten a chat history into the single prompt the browser client sends."""
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if role == "system":
            lines.append(f"System: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
        else:
            lines.append(f"User: {content}")
    return "\n\n".join(lines)
