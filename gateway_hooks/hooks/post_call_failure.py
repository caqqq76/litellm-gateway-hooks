"""`async_post_call_failure_hook`: the request failed at the proxy. Release its concurrency slot, charge nothing."""

from __future__ import annotations

from ..context import Context


async def handle(ctx: Context, request_data: dict) -> None:
    call_id = request_data.get("litellm_call_id") if isinstance(request_data, dict) else None
    if call_id:
        await ctx.limits.release(call_id)
