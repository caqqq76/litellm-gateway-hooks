"""Team / user model routing: `model_routes` aliases and the `allowed_models` allowlist from the config file."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from ..core.config import Principal


@dataclass(frozen=True)
class Routing:
    target_model: str
    allowed: bool


def resolve_routing(user: Principal | None, team: Principal | None, requested_model: str) -> Routing:
    routes = {**(team.model_routes if team else {}), **(user.model_routes if user else {})}
    target = routes.get(requested_model, requested_model)
    allowed_models = (
        user.allowed_models
        if user is not None and user.allowed_models is not None
        else (team.allowed_models if team is not None else None)
    )
    allowed = allowed_models is None or any(fnmatch.fnmatchcase(target, p) for p in allowed_models)
    return Routing(target_model=target, allowed=allowed)
