"""Who a request belongs to: the end-user header or the API key's owner, resolved to a LiteLLM user and team."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from litellm._logging import verbose_proxy_logger as log

from .config import GatewayConfig, Principal, TeamConfig


@dataclass(frozen=True)
class UserLookup:
    """A LiteLLM user found by one of its columns. `user_id` is the canonical identity."""

    found: bool
    user_id: str | None = None
    sso_user_id: str | None = None
    team_id: str | None = None
    team_alias: str | None = None


# (value, columns to try in order) -> the user
UserResolver = Callable[[str, tuple[str, ...]], Awaitable[UserLookup]]


async def prisma_user_resolver(value: str, fields: tuple[str, ...]) -> UserLookup:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise RuntimeError("LiteLLM proxy has no database connection")
    user = None
    for column in fields:
        matches = await prisma_client.db.litellm_usertable.find_many(where={column: value}, take=2)
        if len(matches) > 1:
            log.warning("gateway_hooks: %s=%s matches several users, using %s", column, value, matches[0].user_id)
        if matches:
            user = matches[0]
            break
    if user is None:
        return UserLookup(found=False)
    teams = list(user.teams or [])
    if len(teams) > 1:
        log.warning("gateway_hooks: user %s is in %d teams, using %s", user.user_id, len(teams), teams[0])
    team_id = teams[0] if teams else user.team_id
    team = await prisma_client.db.litellm_teamtable.find_unique(where={"team_id": team_id}) if team_id else None
    return UserLookup(
        found=True,
        user_id=user.user_id,
        sso_user_id=user.sso_user_id,
        team_id=team_id,
        team_alias=getattr(team, "team_alias", None),
    )


@dataclass(frozen=True)
class Identity:
    user_id: str  # key for counters: the LiteLLM user_id when known
    source: str  # "header" or "key"
    lookup: UserLookup
    raw: str  # the header value or key owner as received


class IdentityResolver:
    def __init__(self, config: GatewayConfig, resolver: UserResolver, clock: Callable[[], float]):
        self.config = config
        self.resolver = resolver
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, UserLookup]] = {}
        self._db_down_until = 0.0

    async def _lookup(self, source: str, value: str, fields: tuple[str, ...]) -> UserLookup | None:
        """None = the database could not be asked (fail open). After a failure, lookups pause for 5 s."""
        now = self._clock()
        cached = self._cache.get((source, value))
        if cached is not None and cached[0] > now:
            return cached[1]
        if now < self._db_down_until:
            return None
        try:
            lookup = await asyncio.wait_for(self.resolver(value, fields), self.config.settings.db_timeout_ms / 1000)
        except Exception as e:  # noqa: BLE001  # fail open: no team settings, user/default limits still apply
            log.warning("gateway_hooks: user lookup for %s failed, continuing without team: %r", value, e)
            self._db_down_until = now + 5
            return None
        self._cache[(source, value)] = (now + self.config.settings.team_cache_ttl_seconds, lookup)
        return lookup

    async def identify(self, user_api_key_dict) -> Identity | None:
        """Header first, then the key's owner. None = nobody to attribute the request to."""
        s = self.config.settings
        header_value = user_api_key_dict.end_user_id
        if header_value:
            lookup = await self._lookup("header", header_value, s.user_lookup_fields)
            if lookup is None:
                return Identity(header_value, "header", UserLookup(found=True), header_value)
            return Identity((lookup.found and lookup.user_id) or header_value, "header", lookup, header_value)
        key_user = user_api_key_dict.user_id
        if s.key_user_fallback and key_user and key_user not in s.ignored_key_users:
            lookup = await self._lookup("key", key_user, ("user_id",))
            if lookup is None:
                return Identity(key_user, "key", UserLookup(found=True, user_id=key_user), key_user)
            return Identity(key_user, "key", lookup, key_user)
        return None

    def user_config(self, identity: Identity) -> Principal | None:
        for name in (identity.user_id, identity.lookup.sso_user_id, identity.raw):
            if name is not None and name in self.config.users:
                return self.config.users[name]
        return None

    def team_config(self, lookup: UserLookup) -> tuple[str | None, TeamConfig | None]:
        for name in (lookup.team_id, lookup.team_alias):
            if name is not None and name in self.config.teams:
                return lookup.team_id or name, self.config.teams[name]
        return lookup.team_id, None
