"""Services shared by all hook points of one GatewayHooks instance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .core.calendar import Calendar
from .core.config import GatewayConfig
from .core.identity import IdentityResolver
from .limits.engine import LimitsEngine


@dataclass(frozen=True)
class Context:
    config: GatewayConfig
    calendar: Calendar
    identity: IdentityResolver
    limits: LimitsEngine
    clock: Callable[[], float]
