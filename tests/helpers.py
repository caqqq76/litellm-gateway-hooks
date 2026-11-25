"""Test helpers: a fake clock, a fake LiteLLM user table, and shortcuts that drive GatewayHooks like the proxy does."""

import datetime as dt
from zoneinfo import ZoneInfo

import fakeredis
import pytest

from gateway_hooks import GatewayConfig, GatewayHooks, UserLookup
from gateway_hooks.limits.store import RedisScripts
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.proxy_rate_limit_error import ProxyRateLimitError

TPE = ZoneInfo("Asia/Taipei")


def ts(y, mo, d, h=0, mi=0, s=0) -> float:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=TPE).timestamp()


# 2026-03-02 is a Monday, 2026-03-06 a Friday
MON_10 = ts(2026, 3, 2, 10)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(cfg: dict, t: float, teams: dict[str, str] | None = None, db: list[dict] | None = None):
    """`db` = rows of LiteLLM_UserTable ({user_id, sso_user_id, team}); without it every value is a user_id."""
    teams = teams or {}
    clock = Clock(t)
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def resolver(value: str, fields: tuple[str, ...]) -> UserLookup:
        calls.append((value, fields))
        if db is None:
            return UserLookup(found=True, user_id=value, team_id=teams.get(value))
        for column in fields:
            row = next((r for r in db if r.get(column) == value), None)
            if row is not None:
                return UserLookup(found=True, user_id=row["user_id"], sso_user_id=row.get("sso_user_id"),
                                  team_id=row.get("team"))
        return UserLookup(found=False)

    redis = fakeredis.FakeAsyncRedis()
    limiter = GatewayHooks(
        GatewayConfig.model_validate(cfg), scripts=RedisScripts(redis), user_resolver=resolver, clock=clock
    )
    limiter.resolver_calls = calls
    return limiter, clock


_n = 0


async def call(limiter, user="alice", model="gpt-4o", key_user=None):
    global _n
    _n += 1
    data = {"model": model, "litellm_call_id": f"call-{_n}"}
    auth = UserAPIKeyAuth(end_user_id=user, user_id=key_user)
    return await limiter.async_pre_call_hook(auth, None, data, "acompletion")


async def finish(limiter, data, cost=0.0, tokens=0):
    await limiter.async_log_success_event(
        {"litellm_call_id": data["litellm_call_id"],
         "standard_logging_object": {"response_cost": cost, "total_tokens": tokens}},
        None, None, None,
    )


async def blocked(limiter, **kw) -> ProxyRateLimitError:
    with pytest.raises(ProxyRateLimitError) as exc:
        await call(limiter, **kw)
    return exc.value


def models(**limits):
    return {"defaults": {"models": {"*": limits}}}


# a LiteLLM user table where user_id is a UUID and the NT account lives in sso_user_id
ALICE_UUID = "6f1c2a9e-0b7d-4c3e-9a51-2f8d7e6b4c10"
DB = [
    {"user_id": ALICE_UUID, "sso_user_id": "alice.chen", "team": "eng"},
    {"user_id": "b0b0b0b0-1111-2222-3333-444455556666", "sso_user_id": "bob.lin", "team": "ops"},
]
TEAM_LIMITS = {"teams": {"eng": {"models": {"*": {"rpm": 2}}}, "ops": {"models": {"*": {"rpm": 5}}}}}



async def any_user_found(*args):
    return UserLookup(found=True)
