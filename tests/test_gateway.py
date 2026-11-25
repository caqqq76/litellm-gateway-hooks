from helpers import *  # noqa: F403  # shared fixtures-as-functions, see tests/helpers.py


async def test_fails_open_when_redis_is_down():
    class Broken:
        async def admit(self, keys, args):
            raise ConnectionError("redis down")

        async def charge(self, keys, args):
            raise ConnectionError("redis down")

    limiter = GatewayHooks(GatewayConfig.model_validate(models(rpm=1)), scripts=Broken(),
                                user_resolver=any_user_found, clock=Clock(MON_10))
    d = await call(limiter)
    await call(limiter)
    await finish(limiter, d, cost=1)


async def test_unexpected_limiter_error_lets_the_request_through():
    limiter, _ = make(models(rpm=1), MON_10)

    async def bug(*args):
        raise KeyError("a bug inside the limiter, not a DB or Redis outage")

    limiter.ctx.identity.identify = bug
    data = await call(limiter)
    assert data["model"] == "gpt-4o"
