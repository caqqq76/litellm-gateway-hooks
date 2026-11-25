from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py
from fastapi import HTTPException


async def test_header_with_nt_account_finds_user_and_team_via_sso_user_id():
    limiter, _ = make(TEAM_LIMITS, MON_10, db=DB)
    await call(limiter, user="alice.chen")
    await call(limiter, user="alice.chen")
    err = await blocked(limiter, user="alice.chen")
    assert err.headers["x-ratelimit-limit"] == "2"  # eng team limit, not "no limit"
    assert limiter.resolver_calls[0] == ("alice.chen", ("user_id", "sso_user_id"))


async def test_key_bound_traffic_uses_the_key_owner_and_shares_counters_with_header_traffic():
    limiter, _ = make(TEAM_LIMITS, MON_10, db=DB)
    await call(limiter, user=None, key_user=ALICE_UUID)       # her own key, no header
    await call(limiter, user="alice.chen", key_user="svc-app")  # shared app key + header
    err = await blocked(limiter, user=None, key_user=ALICE_UUID)
    assert err.headers["x-ratelimit-scope"] == "user"
    await call(limiter, user=None, key_user="b0b0b0b0-1111-2222-3333-444455556666")


async def test_user_settings_can_be_keyed_by_nt_account_or_user_id():
    cfg = {**TEAM_LIMITS, "users": {"alice.chen": {"models": {"*": {"rpm": 1}}}}}
    limiter, _ = make(cfg, MON_10, db=DB)
    await call(limiter, user=None, key_user=ALICE_UUID)
    err = await blocked(limiter, user="alice.chen")
    assert err.headers["x-ratelimit-limit"] == "1"


async def test_header_wins_over_key_owner():
    limiter, _ = make(TEAM_LIMITS, MON_10, db=DB)
    for _ in range(5):
        await call(limiter, user="bob.lin", key_user=ALICE_UUID)  # counted as bob (ops rpm 5)
    await call(limiter, user=None, key_user=ALICE_UUID)          # alice still has her own 2


async def test_unknown_header_value_gets_defaults_or_is_denied():
    limiter, _ = make({**TEAM_LIMITS, **models(rpm=1)}, MON_10, db=DB)
    await call(limiter, user="nobody")
    await blocked(limiter, user="nobody")
    strict, _ = make({**TEAM_LIMITS, "settings": {"unknown_user": "deny"}}, MON_10, db=DB)
    with pytest.raises(HTTPException) as exc:
        await call(strict, user="nobody")
    assert exc.value.status_code == 403


async def test_key_user_fallback_can_be_turned_off():
    limiter, _ = make({**models(rpm=1), "settings": {"key_user_fallback": False, "missing_user": "allow"}}, MON_10)
    for _ in range(3):
        await call(limiter, user=None, key_user=ALICE_UUID)  # not identified, not limited


async def test_no_header_and_no_key_owner_is_rejected():
    limiter, _ = make(models(rpm=1), MON_10)
    with pytest.raises(HTTPException) as exc:
        await call(limiter, user=None)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await call(limiter, user=None, key_user="default_user_id")  # master key is not a person
    assert exc.value.status_code == 400


async def test_failed_lookups_are_not_retried_for_five_seconds():
    attempts = []

    async def broken(value, fields):
        attempts.append(value)
        raise RuntimeError("db down")

    clock = Clock(MON_10)
    limiter = GatewayHooks(GatewayConfig.model_validate(models(rpm=10)),
                                scripts=RedisScripts(fakeredis.FakeAsyncRedis()),
                                user_resolver=broken, clock=clock)
    for _ in range(3):
        await call(limiter, user="alice.chen")
    assert attempts == ["alice.chen"]
    clock.t += 6
    await call(limiter, user="alice.chen")
    assert len(attempts) == 2


async def test_fails_open_when_team_lookup_fails():
    async def broken(value, fields):
        raise RuntimeError("db down")

    limiter = GatewayHooks(GatewayConfig.model_validate(models(rpm=1)),
                                scripts=RedisScripts(fakeredis.FakeAsyncRedis()),
                                user_resolver=broken, clock=Clock(MON_10))
    await call(limiter)
    await blocked(limiter)  # user/default limits still enforced
