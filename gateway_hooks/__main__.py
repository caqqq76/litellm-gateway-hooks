"""Validate a gateway-hooks config file and show what it resolves to.

    gateway-hooks-check gateway-hooks.yaml
    gateway-hooks-check gateway-hooks.yaml --user alice.chen --team engineering --model chat-default
"""

import argparse
import sys
import time

from pydantic import ValidationError

from .core.calendar import Calendar
from .core.config import load_config, merge_limits
from .routing.model_routes import resolve_routing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway-hooks-check")
    parser.add_argument("config")
    parser.add_argument("--user", help="users: key in the config file (user_id or sso_user_id)")
    parser.add_argument("--team", help="LiteLLM team_id or team_alias as used in the limits file")
    parser.add_argument("--model")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (ValidationError, ValueError, OSError) as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    try:
        cfg.redis_url_value()
    except ValueError as e:
        print(f"warning: {e}", file=sys.stderr)

    cal = Calendar(cfg.peak_hours)
    period = cal.period_at(time.time())
    print(f"OK: {len(cfg.teams)} teams, {len(cfg.users)} users, windows {list(cfg.windows)}")
    print(f"now: {period.period} ({period.start:%a %Y-%m-%d %H:%M} -> {period.end:%a %Y-%m-%d %H:%M})")

    if args.model:
        user = cfg.users.get(args.user) if args.user else None
        team = cfg.teams.get(args.team) if args.team else None
        routing = resolve_routing(user, team, args.model)
        print(f"model: {args.model} -> {routing.target_model} ({'allowed' if routing.allowed else 'NOT allowed'})")
        eff = merge_limits([user, team, cfg.defaults], routing.target_model, list(cfg.windows))
        print(f"user limits: rpm={eff.rpm} tpm={eff.tpm} concurrency={eff.max_parallel_requests}")
        for p, budgets in eff.budget.items():
            print(f"  budget {p}: {budgets or 'none'}")
        if team is not None and team.team_pool is not None:
            pool = merge_limits([team.team_pool], routing.target_model, list(cfg.windows))
            print(f"team pool: rpm={pool.rpm} tpm={pool.tpm} concurrency={pool.max_parallel_requests} budget={pool.budget}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
