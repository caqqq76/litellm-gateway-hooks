"""`async_pre_call_hook`: before the router runs. Identify the user, apply team/user routing, admit against limits."""

from __future__ import annotations

from fastapi import HTTPException

from ..context import Context
from ..core.config import merge_limits
from ..limits.engine import Scope, forbidden
from ..routing.model_routes import resolve_routing


async def handle(ctx: Context, user_api_key_dict, data: dict) -> dict:
    requested_model = data.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        return data

    identity = await ctx.identity.identify(user_api_key_dict)
    if identity is None:
        if ctx.config.settings.missing_user == "deny":
            raise HTTPException(
                status_code=400,
                detail={"error": "No user: send the end-user header or use an API key that belongs to a user"},
            )
        return data
    if not identity.lookup.found and ctx.config.settings.unknown_user == "deny":
        raise forbidden(f"Unknown user {identity.raw}")

    user_cfg = ctx.identity.user_config(identity)
    team_id, team_cfg = ctx.identity.team_config(identity.lookup)

    routing = resolve_routing(user_cfg, team_cfg, requested_model)
    if not routing.allowed:
        raise forbidden(f"Model {requested_model} is not allowed for user {identity.user_id}")
    model = routing.target_model
    data["model"] = model

    windows = list(ctx.config.windows)
    scopes = [Scope("user", identity.user_id, merge_limits([user_cfg, team_cfg, ctx.config.defaults], model, windows))]
    if team_id is not None and team_cfg is not None and team_cfg.team_pool is not None:
        scopes.append(Scope("team", team_id, merge_limits([team_cfg.team_pool], model, windows)))

    member = str(data.get("litellm_call_id") or f"{identity.user_id}:{ctx.clock()}")
    await ctx.limits.admit(scopes, model, member)
    return data
