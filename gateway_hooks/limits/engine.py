"""Limit enforcement: build the counter plan for a request, admit it atomically, charge or release it afterwards."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import HTTPException

from litellm._logging import verbose_proxy_logger as log
from litellm.proxy.common_utils.proxy_rate_limit_error import ProxyRateLimitError

from ..core.calendar import Calendar, PeriodInfo
from ..core.config import EffectiveLimits, GatewayConfig
from .store import ChargeItem, Entry, RedisScripts, ScriptRunner, admit, charge, check_budgets, scope_key

# how many peak / off-peak periods a budget retry-after looks ahead (8 = at least two working days)
MAX_PERIODS_AHEAD = 8


@dataclass
class Admission:
    """What one admitted request must charge (on success) and release (always) once it ends."""

    created_at: float
    member: str
    charges: list[ChargeItem] = field(default_factory=list)
    releases: list[ChargeItem] = field(default_factory=list)


@dataclass(frozen=True)
class Scope:
    kind: str  # "user" or "team"
    id: str
    limits: EffectiveLimits


def forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"error": message})


class LimitsEngine:
    def __init__(self, config: GatewayConfig, calendar: Calendar, scripts: ScriptRunner | None,
                 clock: Callable[[], float]):
        self.config = config
        self.calendar = calendar
        self._scripts = scripts
        self._clock = clock
        self._admissions: dict[str, Admission] = {}
        self._last_prune = 0.0
        self._redis_warm = False

    # ---------- Redis ----------

    def _runner(self) -> ScriptRunner:
        if self._scripts is None:
            import redis.asyncio as aioredis

            s = self.config.settings
            client = aioredis.from_url(
                self.config.redis_url_value(),
                socket_timeout=s.redis_timeout_ms / 1000,
                socket_connect_timeout=s.redis_connect_timeout_ms / 1000,
            )
            self._scripts = RedisScripts(client)
        return self._scripts

    async def _redis(self, aw: Awaitable, timeout_ms: int):
        if not self._redis_warm:
            timeout_ms = max(timeout_ms, self.config.settings.redis_connect_timeout_ms)
        result = await asyncio.wait_for(aw, timeout=timeout_ms / 1000)
        self._redis_warm = True
        return result

    # ---------- plan ----------

    def _entries_for(self, scope: Scope, model: str, period: PeriodInfo, now: float
                     ) -> tuple[list[Entry], list[ChargeItem], list[ChargeItem]]:
        s = self.config.settings
        p = s.key_prefix
        limits = scope.limits
        entries: list[Entry] = []
        charges: list[ChargeItem] = []
        releases: list[ChargeItem] = []
        meta = {"scope": scope.kind, "model": model}

        if limits.rpm is not None:
            entries.append(Entry("rpm", scope_key(p, "rpm", scope.kind, scope.id, model), limits.rpm,
                                 s.rpm_window_seconds * 1000, **meta))
        if limits.tpm is not None:
            key = scope_key(p, "tpm", scope.kind, scope.id, model)
            window_ms, bucket_ms = s.tpm_window_seconds * 1000, s.tpm_bucket_seconds * 1000
            entries.append(Entry("tpm", key, limits.tpm, window_ms, bucket_ms, **meta))
            charges.append(ChargeItem("tpm", key, 0, window_ms, bucket_ms))
        if limits.max_parallel_requests is not None:
            key = scope_key(p, "conc", scope.kind, scope.id, model)
            entries.append(Entry("conc", key, limits.max_parallel_requests, s.lease_ttl_seconds * 1000,
                                 s.concurrency_retry_after_ms, **meta))
            releases.append(ChargeItem("conc", key, 0, 0))

        period_end_ms = int(period.end.timestamp() * 1000)
        for window_name, limit in limits.budget.get(period.period, {}).items():
            spec = self.config.windows[window_name]
            bmeta = {**meta, "window": window_name, "period": period.period}
            if spec.mode == "rolling":
                key = scope_key(p, "b", scope.kind, scope.id, model, period.period, window_name)
                window_ms, bucket_ms = spec.seconds * 1000, spec.bucket_seconds * 1000
                entries.append(Entry("broll", key, limit, window_ms, bucket_ms, **bmeta))
                charges.append(ChargeItem("broll", key, 0, window_ms, bucket_ms))
            else:
                w = self.calendar.fixed_window(now, spec, period)
                key = scope_key(p, "b", scope.kind, scope.id, model, period.period, window_name,
                                str(int(w.start.timestamp())))
                reset_ms = min(int(w.end.timestamp() * 1000), period_end_ms)
                entries.append(Entry("bfix", key, limit, reset_ms, **bmeta))
                charges.append(ChargeItem("bfix", key, 0, int(w.end.timestamp() * 1000) + 3_600_000))
        return entries, charges, releases

    # ---------- errors ----------

    def rate_limit_error(self, entry: Entry, wait_ms: int, current: float, now: float) -> ProxyRateLimitError:
        reset_at = self.calendar.local(math.ceil(now + wait_ms / 1000)).isoformat(timespec="seconds")
        what = f"{entry.limit_type}" + (f" {entry.window} ({entry.period})" if entry.window else "")
        message = (
            f"Rate limit exceeded: {entry.scope} {what} limit for model {entry.model} "
            f"(limit {entry.limit:g}, current {current:g}). Retry after {reset_at}"
        )
        headers = {
            "retry-after": str(max(1, math.ceil(wait_ms / 1000))),
            "retry-after-ms": str(wait_ms),
            "x-ratelimit-limit-type": entry.limit_type,
            "x-ratelimit-scope": entry.scope,
            "x-ratelimit-model": entry.model,
            "x-ratelimit-limit": f"{entry.limit:g}",
            "x-ratelimit-reset-at": reset_at,
        }
        if entry.window:
            headers["x-ratelimit-window"] = entry.window
            headers["x-ratelimit-period"] = entry.period or ""
        return ProxyRateLimitError(
            detail={
                "error": message,
                "limit_type": entry.limit_type,
                "scope": entry.scope,
                "model": entry.model,
                "window": entry.window,
                "period": entry.period,
                "limit": entry.limit,
                "current": current,
                "retry_after_ms": wait_ms,
                "reset_at": reset_at,
            },
            headers=headers,
            model=entry.model,
        )

    # ---------- lifecycle ----------

    async def admit(self, scopes: list[Scope], model: str, member: str) -> None:
        """Raise 403/429 when the request may not run; otherwise record it as admitted (fails open on Redis errors)."""
        now = self._clock()
        period = self.calendar.period_at(now)
        entries: list[Entry] = []
        admission = Admission(created_at=now, member=member)
        for scope in scopes:
            limits = scope.limits
            if 0 in (limits.rpm, limits.tpm, limits.max_parallel_requests):
                raise forbidden(f"Model {model} is disabled for {scope.kind} {scope.id}")
            e, c, r = self._entries_for(scope, model, period, now)
            zero_budget = next((x for x in e if x.limit_type == "budget" and x.limit == 0), None)
            if zero_budget is not None:
                to_period_end = max(1, int((period.end.timestamp() - now) * 1000))
                wait_ms = await self._budget_wait(zero_budget, to_period_end, scopes, model, period, now)
                raise self.rate_limit_error(zero_budget, wait_ms, 0, now)
            entries += e
            admission.charges += c
            admission.releases += r

        try:
            blocked = await self._redis(admit(self._runner(), entries, int(now * 1000), member),
                                        self.config.settings.redis_timeout_ms)
        except Exception as e:  # noqa: BLE001  # fail open by design
            log.warning("gateway_hooks: admit failed, letting request through: %r", e)
            return

        if blocked is not None:
            wait_ms = await self._budget_wait(blocked.entry, blocked.wait_ms, scopes, model, period, now)
            raise self.rate_limit_error(blocked.entry, wait_ms, blocked.current, now)

        if admission.charges or admission.releases:
            self._prune(now)
            self._admissions[member] = admission

    async def _budget_wait(self, entry: Entry, wait_ms: int, scopes: list[Scope], model: str,
                           period: PeriodInfo, now: float) -> int:
        """How long until a request blocked by `entry` is really admitted, so a client that waits is not refused again.

        Each period has its own budget pools, so a budget wait that reaches the end of this period ends there,
        unless the next period's pools are spent too or set to 0. Then look ahead, period by period.
        """
        if entry.limit_type != "budget" or now + wait_ms / 1000 < period.end.timestamp():
            return wait_ms
        t = period.end.timestamp()
        for _ in range(MAX_PERIODS_AHEAD):
            nxt = self.calendar.period_at(t)
            budgets = [e for s in scopes for e in self._entries_for(s, model, nxt, t)[0] if e.limit_type == "budget"]
            if any(e.limit == 0 for e in budgets):
                t = nxt.end.timestamp()
                continue
            try:
                blocked = await self._redis(check_budgets(self._runner(), budgets, int(t * 1000)),
                                            self.config.settings.redis_timeout_ms)
            except Exception as e:  # noqa: BLE001  # the 429 still goes out, with the wait up to the period end
                log.warning("gateway_hooks: budget look-ahead failed, retry-after stops at a period end: %r", e)
                break
            if blocked is None:
                break
            if t + blocked.wait_ms / 1000 < nxt.end.timestamp():
                t += blocked.wait_ms / 1000
                break
            t = nxt.end.timestamp()
        return max(1, int((t - now) * 1000))

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 60:
            return
        self._last_prune = now
        cutoff = now - self.config.settings.lease_ttl_seconds
        for call_id in [k for k, a in self._admissions.items() if a.created_at < cutoff]:
            self._admissions.pop(call_id, None)

    async def charge(self, member: str, cost: float, tokens: float) -> None:
        """The request finished: add its actual cost and tokens, free its concurrency slot."""
        admission = self._admissions.pop(member, None)
        if admission is None:
            return
        items = [
            ChargeItem(i.kind, i.key, tokens if i.kind == "tpm" else cost, i.p1, i.p2) for i in admission.charges
        ] + admission.releases
        await self._settle(items, member)

    async def release(self, member: str) -> None:
        """The request failed: free its concurrency slot, charge nothing."""
        admission = self._admissions.pop(member, None)
        if admission is not None:
            await self._settle(admission.releases, member)

    async def _settle(self, items: list[ChargeItem], member: str) -> None:
        try:
            await self._redis(charge(self._runner(), items, int(self._clock() * 1000), member),
                              self.config.settings.redis_timeout_ms * 4)
        except Exception as e:  # noqa: BLE001  # never fail a finished request over accounting
            log.warning("gateway_hooks: charge failed for %s: %r", member, e)
