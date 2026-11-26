from gateway_hooks import GatewayConfig
from gateway_hooks.core.config import merge_limits
import pytest


def test_user_overrides_team_field_by_field_and_minus_one_lifts():
    cfg = GatewayConfig.model_validate({
        "defaults": {"models": {"*": {"rpm": 5, "tpm": 500}}},
        "teams": {"eng": {"models": {"gpt-4o": {"rpm": 10, "max_parallel_requests": 3,
                                                "budget": {"peak": {"1d": 5}}}}}},
        "users": {"alice": {"models": {"*": {"rpm": 99, "tpm": -1}}}},
    })
    eff = merge_limits([cfg.users["alice"], cfg.teams["eng"], cfg.defaults], "gpt-4o", list(cfg.windows))
    assert (eff.rpm, eff.tpm, eff.max_parallel_requests) == (99, None, 3)
    assert eff.budget["peak"] == {"1d": 5.0}


def test_unknown_budget_window_rejected():
    with pytest.raises(ValueError, match="unknown windows"):
        GatewayConfig.model_validate({"defaults": {"models": {"*": {"budget": {"peak": {"2h": 1}}}}}})


def test_limit_model_maps_members_to_their_group():
    cfg = GatewayConfig.model_validate({"model_groups": {"gpt-4o": ["gpt-4o", "gpt-4o-b"]}})
    assert [cfg.limit_model(m) for m in ("gpt-4o", "gpt-4o-b", "o1")] == ["gpt-4o", "gpt-4o", "o1"]


@pytest.mark.parametrize("raw, message", [
    ({"model_groups": {"g": []}}, "list at least one model"),
    ({"model_groups": {"g1": ["a"], "g2": ["a"]}}, "a is in both g1 and g2"),
    ({"model_groups": {"g1": ["g2"], "g2": ["b"]}}, "g2 is a group and also a member of g1"),
    # a member's own limits would never apply, so they are refused instead of silently ignored
    ({"model_groups": {"g": ["a", "b"]}, "users": {"u": {"models": {"b": {"rpm": 1}}}}},
     "users.u.models.b: b is in model group g"),
    ({"model_groups": {"g": ["a"]}, "teams": {"t": {"team_pool": {"models": {"a": {"rpm": 1}}}}}},
     "teams.t.team_pool.models.a"),
])
def test_invalid_model_groups_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        GatewayConfig.model_validate(raw)
