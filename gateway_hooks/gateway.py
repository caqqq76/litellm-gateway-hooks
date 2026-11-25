"""The single CustomLogger LiteLLM loads. Each method is one LiteLLM hook point and delegates to gateway_hooks/hooks/.

LiteLLM only runs some hook points (async_pre_call_hook, the streaming and response-header hooks) when the method is
defined on the registered class itself, so every hook point used lives here, not on a base class.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import HTTPException

from litellm._logging import verbose_proxy_logger as log
from litellm.integrations.custom_logger import CustomLogger

from .context import Context
from .core.calendar import Calendar
from .core.config import GatewayConfig
from .core.identity import IdentityResolver, UserResolver, prisma_user_resolver
from .hooks import log_success_event, post_call_failure, pre_call
from .limits.engine import LimitsEngine
from .limits.store import ScriptRunner


class GatewayHooks(CustomLogger):
    def __init__(
        self,
        config: GatewayConfig,
        scripts: ScriptRunner | None = None,
        user_resolver: UserResolver = prisma_user_resolver,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__()
        calendar = Calendar(config.peak_hours)
        self.ctx = Context(
            config=config,
            calendar=calendar,
            identity=IdentityResolver(config, user_resolver, clock),
            limits=LimitsEngine(config, calendar, scripts, clock),
            clock=clock,
        )

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            return await pre_call.handle(self.ctx, user_api_key_dict, data)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001  # a gateway-hooks bug must never fail a request: fail open
            log.exception("gateway_hooks: unexpected error, letting request through: %r", e)
            return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            await log_success_event.handle(self.ctx, kwargs)
        except Exception as e:  # noqa: BLE001  # never fail a finished request
            log.exception("gateway_hooks: unexpected error in success handling: %r", e)

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict,
                                           traceback_str=None):
        try:
            await post_call_failure.handle(self.ctx, request_data)
        except Exception as e:  # noqa: BLE001  # never replace the original error
            log.exception("gateway_hooks: unexpected error in failure handling: %r", e)
        return None
