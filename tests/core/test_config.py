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
