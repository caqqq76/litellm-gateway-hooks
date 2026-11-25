from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py
from fastapi import HTTPException


async def test_team_routing_and_allowlist_with_user_override():
    cfg = {
        "teams": {"eng": {"model_routes": {"chat": "gpt-4o-mini"}, "allowed_models": ["gpt-4o-mini"]}},
        "users": {"alice": {"model_routes": {"chat": "gpt-4o"}, "allowed_models": ["gpt-4o*"]}},
    }
    limiter, _ = make(cfg, MON_10, teams={"alice": "eng", "bob": "eng"})
    assert (await call(limiter, user="bob", model="chat"))["model"] == "gpt-4o-mini"
    assert (await call(limiter, user="alice", model="chat"))["model"] == "gpt-4o"
    with pytest.raises(HTTPException) as exc:
        await call(limiter, user="bob", model="gpt-4o")
    assert exc.value.status_code == 403
