from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py


async def test_fixed_hour_budget_resets_on_the_hour():
    limiter, clock = make(models(budget={"peak": {"1h": 1.0}}), ts(2026, 3, 2, 10, 40))
    await finish(limiter, await call(limiter), cost=1.2)
    err = await blocked(limiter)
    assert err.headers["x-ratelimit-window"] == "1h"
    assert err.headers["x-ratelimit-period"] == "peak"
    assert int(err.headers["retry-after-ms"]) == 20 * 60 * 1000
    assert err.headers["x-ratelimit-reset-at"] == "2026-03-02T11:00:00+08:00"
    clock.t = ts(2026, 3, 2, 11)
    await call(limiter)


async def test_rolling_15m_budget():
    limiter, clock = make(models(budget={"peak": {"15m": 1.0}}), MON_10)
    await finish(limiter, await call(limiter), cost=0.6)
    clock.t += 300
    await finish(limiter, await call(limiter), cost=0.6)
    err = await blocked(limiter)
    # the first 0.6 (bucket 10:00) leaves the 15-minute window at 10:16
    assert err.headers["x-ratelimit-reset-at"] == "2026-03-02T10:16:00+08:00"
    clock.t = ts(2026, 3, 2, 10, 16)
    await call(limiter)


async def test_peak_and_off_peak_are_separate_pools_and_wait_is_capped_at_period_end():
    limiter, clock = make(models(budget={"peak": {"1d": 1.0}, "off_peak": {"1d": 1.0}}), ts(2026, 3, 2, 17, 0))
    await finish(limiter, await call(limiter), cost=5)
    err = await blocked(limiter)
    assert err.headers["x-ratelimit-reset-at"] == "2026-03-02T18:00:00+08:00"  # not midnight
    clock.t = ts(2026, 3, 2, 18)
    await finish(limiter, await call(limiter), cost=5)
    err = await blocked(limiter)
    assert err.headers["x-ratelimit-period"] == "off_peak"
    assert err.headers["x-ratelimit-reset-at"] == "2026-03-03T00:00:00+08:00"


async def test_zero_peak_budget_means_off_peak_only():
    limiter, clock = make({"defaults": {"models": {"o1": {"budget": {"peak": {"1d": 0}}}}}}, MON_10)
    err = await blocked(limiter, model="o1")
    assert err.headers["x-ratelimit-reset-at"] == "2026-03-02T18:00:00+08:00"
    clock.t = ts(2026, 3, 2, 18, 30)
    await call(limiter, model="o1")


async def test_team_pool_is_shared_by_members():
    cfg = {"teams": {"eng": {"team_pool": {"models": {"*": {"budget": {"peak": {"1d": 1.0}}}}}}}}
    limiter, _ = make(cfg, MON_10, teams={"alice": "eng", "bob": "eng", "carol": "ops"})
    await finish(limiter, await call(limiter, user="alice"), cost=1.0)
    err = await blocked(limiter, user="bob")
    assert err.headers["x-ratelimit-scope"] == "team"
    await call(limiter, user="carol")
