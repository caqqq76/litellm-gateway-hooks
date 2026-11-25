"""`async_log_success_event`: after a successful call (for streams, once the stream ends). Charge cost and tokens."""

from __future__ import annotations

from ..context import Context


async def handle(ctx: Context, kwargs: dict) -> None:
    call_id = kwargs.get("litellm_call_id")
    if not call_id:
        return
    slo = kwargs.get("standard_logging_object") or {}
    cost = float(slo.get("response_cost") or kwargs.get("response_cost") or 0.0)
    tokens = float(slo.get("total_tokens") or 0)
    await ctx.limits.charge(call_id, cost, tokens)
