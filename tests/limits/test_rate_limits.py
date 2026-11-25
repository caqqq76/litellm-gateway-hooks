from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py


async def test_rpm_is_rolling_and_retry_after_is_exact():
    limiter, clock = make(models(rpm=2), MON_10)
    await call(limiter)
    clock.t += 10
    await call(limiter)
    clock.t += 20
    err = await blocked(limiter)
    assert err.status_code == 429
    assert err.headers["x-ratelimit-limit-type"] == "rpm"
    assert int(err.headers["retry-after-ms"]) == 30_001  # first request leaves the window at +60s
    assert err.headers["retry-after"] == "31"
    clock.t += 30.002
    await call(limiter)


async def test_tpm_check_then_charge():
    limiter, clock = make(models(tpm=100), MON_10)
    d = await call(limiter)
    await call(limiter)  # still allowed: nothing charged yet
    await finish(limiter, d, tokens=150)
    err = await blocked(limiter)
    assert err.headers["x-ratelimit-limit-type"] == "tpm"
    clock.t += int(err.headers["retry-after-ms"]) / 1000
    await call(limiter)


async def test_concurrency_released_on_success_and_on_failure_and_by_lease_expiry():
    limiter, clock = make({**models(max_parallel_requests=1), "settings": {"lease_ttl_seconds": 30}}, MON_10)
    d = await call(limiter)
    err = await blocked(limiter)
    assert err.headers["x-ratelimit-limit-type"] == "concurrency"
    await finish(limiter, d)
    d = await call(limiter)
    await limiter.async_post_call_failure_hook(d, RuntimeError("upstream"), UserAPIKeyAuth())
    await call(limiter)  # never released (e.g. cancelled stream)
    await blocked(limiter)
    clock.t += 31
    await call(limiter)


async def test_limits_are_per_model_and_per_user():
    limiter, _ = make({"defaults": {"models": {"gpt-4o": {"rpm": 1}}}}, MON_10)
    await call(limiter)
    await blocked(limiter)
    await call(limiter, model="gpt-4o-mini")
    await call(limiter, user="bob")
