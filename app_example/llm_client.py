"""Reference client for applications calling the LiteLLM proxy with per-user limits."""

import asyncio
import os
import random

import litellm

PROXY_URL = os.environ.get("LLM_PROXY_URL", "http://localhost:4000")
APP_API_KEY = os.environ.get("LLM_PROXY_API_KEY", "")
END_USER_HEADER = os.environ.get("LLM_END_USER_HEADER", "x-end-user-id")
MAX_AUTO_WAIT_S = 60
MAX_ATTEMPTS = 3


class LimitReached(Exception):
    def __init__(self, limit_type, reset_at, model, wait_s):
        super().__init__(f"{limit_type} limit reached for {model}, retry after {reset_at}")
        self.limit_type, self.reset_at, self.model, self.wait_s = limit_type, reset_at, model, wait_s


def _headers(e):
    return getattr(e, "litellm_response_headers", None) or getattr(getattr(e, "response", None), "headers", None) or {}


def _wait_seconds(h):
    if h.get("retry-after-ms"):
        return float(h["retry-after-ms"]) / 1000
    if h.get("retry-after"):
        return float(h["retry-after"])
    return None


async def chat(end_user_id: str, model: str, messages: list, **kwargs):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await litellm.acompletion(
                model=f"litellm_proxy/{model}",
                api_base=PROXY_URL,
                api_key=APP_API_KEY,
                messages=messages,
                extra_headers={END_USER_HEADER: end_user_id},
                max_retries=0,
                **kwargs,
            )
        except litellm.RateLimitError as e:
            h = _headers(e)
            wait = _wait_seconds(h)
            if wait is None:
                wait = 2**attempt
            if wait > MAX_AUTO_WAIT_S or attempt == MAX_ATTEMPTS - 1:
                raise LimitReached(h.get("x-ratelimit-limit-type"), h.get("x-ratelimit-reset-at"), model, wait) from e
            await asyncio.sleep(wait + random.uniform(0, 0.5))
