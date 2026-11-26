from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py
from fastapi import HTTPException

# gpt-4o is load-balanced per user over two LiteLLM model groups, gpt-4o-a and gpt-4o-b
GROUPS = {"model_groups": {"gpt-4o": ["gpt-4o-a", "gpt-4o-b"]}}
ROUTES = {"users": {"alice": {"model_routes": {"gpt-4o": "gpt-4o-a"}},
                    "bob": {"model_routes": {"gpt-4o": "gpt-4o-b"}}}}


async def test_members_share_one_budget():
    cfg = {**GROUPS, **ROUTES, "defaults": {"models": {"gpt-4o": {"budget": {"peak": {"1d": 1.0}}}}}}
    limiter, _ = make(cfg, MON_10)
    data = await call(limiter, user="alice", model="gpt-4o")
    assert data["model"] == "gpt-4o-a"  # routing still picks the real model group
    await finish(limiter, data, cost=1.0)
    # sending the other deployment's internal name directly does not open a second budget
    err = await blocked(limiter, user="alice", model="gpt-4o-b")
    assert err.headers["x-ratelimit-limit-type"] == "budget"
    assert err.headers["x-ratelimit-model"] == "gpt-4o"
    await blocked(limiter, user="alice", model="gpt-4o-a")
    await call(limiter, user="bob", model="gpt-4o")  # per user: bob's pool is untouched


async def test_members_share_rpm_tpm_and_concurrency():
    cfg = {**GROUPS, "defaults": {"models": {"gpt-4o": {"rpm": 2, "max_parallel_requests": 5}}}}
    limiter, _ = make(cfg, MON_10)
    await call(limiter, model="gpt-4o-a")
    await call(limiter, model="gpt-4o-b")
    err = await blocked(limiter, model="gpt-4o-a")
    assert err.headers["x-ratelimit-limit-type"] == "rpm"

    cfg = {**GROUPS, "defaults": {"models": {"gpt-4o": {"max_parallel_requests": 1}}}}
    limiter, _ = make(cfg, MON_10)
    d = await call(limiter, model="gpt-4o-a")
    err = await blocked(limiter, model="gpt-4o-b")
    assert err.headers["x-ratelimit-limit-type"] == "concurrency"
    await finish(limiter, d)  # releasing the slot of gpt-4o-a frees the group
    await call(limiter, model="gpt-4o-b")


async def test_group_limits_come_from_team_and_user_layers_and_wildcard():
    cfg = {**GROUPS,
           "defaults": {"models": {"*": {"rpm": 1}}},
           "teams": {"eng": {"models": {"gpt-4o": {"rpm": 3}}}}}
    limiter, _ = make(cfg, MON_10, teams={"alice": "eng"})
    for _ in range(3):
        await call(limiter, user="alice", model="gpt-4o-b")
    assert (await blocked(limiter, user="alice", model="gpt-4o-a")).headers["x-ratelimit-limit"] == "3"
    await call(limiter, user="carol", model="gpt-4o-a")  # no team: the "*" default, counted for the group
    await blocked(limiter, user="carol", model="gpt-4o-b")


async def test_group_name_may_be_a_member_and_other_models_stay_separate():
    cfg = {"model_groups": {"gpt-4o": ["gpt-4o", "gpt-4o-b"]}, "defaults": {"models": {"*": {"rpm": 1}}}}
    limiter, _ = make(cfg, MON_10)
    await call(limiter, model="gpt-4o")
    await blocked(limiter, model="gpt-4o-b")
    await call(limiter, model="gpt-4o-mini")


async def test_team_pool_counts_the_group():
    cfg = {**GROUPS, "teams": {"eng": {"team_pool": {"models": {"gpt-4o": {"budget": {"peak": {"1d": 1.0}}}}}}}}
    limiter, _ = make(cfg, MON_10, teams={"alice": "eng", "bob": "eng"})
    await finish(limiter, await call(limiter, user="alice", model="gpt-4o-a"), cost=1.0)
    err = await blocked(limiter, user="bob", model="gpt-4o-b")
    assert (err.headers["x-ratelimit-scope"], err.headers["x-ratelimit-model"]) == ("team", "gpt-4o")


async def test_allowed_models_still_checks_the_real_model():
    cfg = {**GROUPS, "users": {"alice": {"allowed_models": ["gpt-4o-a"]}}}
    limiter, _ = make(cfg, MON_10)
    await call(limiter, model="gpt-4o-a")
    with pytest.raises(HTTPException) as exc:
        await call(limiter, model="gpt-4o-b")
    assert exc.value.status_code == 403
