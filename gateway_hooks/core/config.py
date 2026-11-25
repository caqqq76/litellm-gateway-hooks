"""gateway-hooks config file schema, loading, and the defaults -> team -> user merge of limits."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Period = Literal["peak", "off_peak"]
PERIODS: tuple[Period, ...] = ("peak", "off_peak")
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# -1 means "explicitly unlimited" (use it to lift a limit a lower layer set). 0 blocks the model.
UNLIMITED = -1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WindowSpec(_Strict):
    seconds: int = Field(gt=0)
    mode: Literal["rolling", "fixed"]
    # rolling only: size of the time buckets the window is summed from
    bucket_seconds: int = Field(default=60, gt=0)
    # fixed only: "midnight" = aligned to local midnight, "period_start" = aligned to the start of
    # the current peak/off-peak period (e.g. 4h windows 09-13, 13-17, 17-18 / 18-22, 22-02, 02-06, 06-09)
    anchor: Literal["midnight", "period_start"] = "midnight"

    @model_validator(mode="after")
    def _check(self) -> "WindowSpec":
        if self.mode == "rolling" and self.seconds % self.bucket_seconds:
            raise ValueError("rolling window seconds must be a multiple of bucket_seconds")
        return self


DEFAULT_WINDOWS: dict[str, WindowSpec] = {
    "15m": WindowSpec(seconds=900, mode="rolling", bucket_seconds=60),
    "1h": WindowSpec(seconds=3600, mode="fixed", anchor="midnight"),
    "1d": WindowSpec(seconds=86400, mode="fixed", anchor="midnight"),
}


class PeakHours(_Strict):
    timezone: str = "Asia/Taipei"
    weekdays: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")
    start: dt.time = dt.time(9, 0)
    end: dt.time = dt.time(18, 0)
    holidays: frozenset[dt.date] = frozenset()
    extra_workdays: frozenset[dt.date] = frozenset()

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [d for d in v if d.lower() not in WEEKDAY_NAMES]
        if bad:
            raise ValueError(f"unknown weekdays {bad}, use {WEEKDAY_NAMES}")
        return tuple(d.lower() for d in v)

    @model_validator(mode="after")
    def _check(self) -> "PeakHours":
        if self.start >= self.end:
            raise ValueError("peak_hours.start must be before peak_hours.end (overnight peaks are not supported)")
        return self


class ModelLimits(_Strict):
    rpm: int | None = None
    tpm: int | None = None
    max_parallel_requests: int | None = None
    # budget[period][window_name] = USD
    budget: dict[Period, dict[str, float]] = Field(default_factory=dict)


class LimitSet(_Strict):
    models: dict[str, ModelLimits] = Field(default_factory=dict)


class Principal(LimitSet):
    """A team or a user. Model routing and access live here; limits live in `models`."""

    model_routes: dict[str, str] = Field(default_factory=dict)
    allowed_models: tuple[str, ...] | None = None


class TeamConfig(Principal):
    # optional aggregate pool shared by every member of the team
    team_pool: LimitSet | None = None


class Settings(_Strict):
    redis_url: str = "os.environ/GATEWAY_HOOKS_REDIS_URL"
    key_prefix: str = "gwh"
    redis_timeout_ms: int = Field(default=50, gt=0)
    # until the first successful call: covers connecting and loading the Lua scripts
    redis_connect_timeout_ms: int = Field(default=1000, gt=0)
    db_timeout_ms: int = Field(default=200, gt=0)
    team_cache_ttl_seconds: int = Field(default=60, ge=0)
    rpm_window_seconds: int = Field(default=60, gt=0)
    tpm_window_seconds: int = Field(default=60, gt=0)
    tpm_bucket_seconds: int = Field(default=5, gt=0)
    # a concurrency slot is reclaimed after this long even if the request never reported back
    # (e.g. a client cancelling a stream skips LiteLLM's success/failure callbacks)
    lease_ttl_seconds: int = Field(default=900, gt=0)
    concurrency_retry_after_ms: int = Field(default=1000, gt=0)
    # the end-user header value is matched against these LiteLLM_UserTable columns, in order
    user_lookup_fields: tuple[Literal["user_id", "sso_user_id", "user_email"], ...] = ("user_id", "sso_user_id")
    # no end-user header -> the request belongs to the user the API key is bound to
    key_user_fallback: bool = True
    # key owners that are not people (the master key authenticates as default_user_id)
    ignored_key_users: tuple[str, ...] = ("default_user_id",)
    # neither a header nor a usable key owner: deny (400) | allow (no limits)
    missing_user: Literal["deny", "allow"] = "deny"
    unknown_user: Literal["defaults", "deny"] = "defaults"

    @field_validator("user_lookup_fields")
    @classmethod
    def _lookup_fields(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("user_lookup_fields needs at least one column")
        return v


class GatewayConfig(_Strict):
    settings: Settings = Settings()
    peak_hours: PeakHours = PeakHours()
    windows: dict[str, WindowSpec] = Field(default_factory=lambda: dict(DEFAULT_WINDOWS))
    defaults: LimitSet = LimitSet()
    teams: dict[str, TeamConfig] = Field(default_factory=dict)
    users: dict[str, Principal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _budget_windows_exist(self) -> "GatewayConfig":
        def check(where: str, ls: LimitSet | None) -> None:
            if ls is None:
                return
            for model, ml in ls.models.items():
                for period, per_window in ml.budget.items():
                    unknown = set(per_window) - set(self.windows)
                    if unknown:
                        raise ValueError(
                            f"{where}.models.{model}.budget.{period}: unknown windows {sorted(unknown)}, "
                            f"defined windows are {sorted(self.windows)}"
                        )

        check("defaults", self.defaults)
        for name, team in self.teams.items():
            check(f"teams.{name}", team)
            check(f"teams.{name}.team_pool", team.team_pool)
        for name, user in self.users.items():
            check(f"users.{name}", user)
        return self

    def redis_url_value(self) -> str:
        return resolve_env(self.settings.redis_url)


def resolve_env(value: str) -> str:
    if value.startswith("os.environ/"):
        name = value.removeprefix("os.environ/")
        resolved = os.environ.get(name)
        if not resolved:
            raise ValueError(f"environment variable {name} is not set")
        return resolved
    return value


def load_config(path: str | Path) -> GatewayConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return GatewayConfig.model_validate(raw)


@dataclass(frozen=True)
class EffectiveLimits:
    """Limits for one (scope, model) after merging. None means no limit."""

    rpm: int | None
    tpm: int | None
    max_parallel_requests: int | None
    budget: dict[Period, dict[str, float]]

    @property
    def blocked(self) -> bool:
        values = [self.rpm, self.tpm, self.max_parallel_requests]
        values += [v for per in self.budget.values() for v in per.values()]
        return any(v == 0 for v in values)


def _lookup(layers: list[LimitSet | None], model: str) -> list[ModelLimits]:
    """Most specific first: within each layer the exact model beats the "*" wildcard."""
    out: list[ModelLimits] = []
    for layer in layers:
        if layer is None:
            continue
        exact = layer.models.get(model)
        if exact is not None:
            out.append(exact)
        wildcard = layer.models.get("*")
        if wildcard is not None:
            out.append(wildcard)
    return out


def _first(values: list[float | int | None]) -> float | int | None:
    for v in values:
        if v is not None:
            return None if v == UNLIMITED else v
    return None


def merge_limits(layers: list[LimitSet | None], model: str, window_names: list[str]) -> EffectiveLimits:
    """`layers` ordered highest priority first (user, team, defaults). Merged field by field."""
    candidates = _lookup(layers, model)
    budget: dict[Period, dict[str, float]] = {}
    for period in PERIODS:
        per: dict[str, float] = {}
        for window in window_names:
            v = _first([c.budget.get(period, {}).get(window) for c in candidates])
            if v is not None:
                per[window] = float(v)
        budget[period] = per
    return EffectiveLimits(
        rpm=_first([c.rpm for c in candidates]),
        tpm=_first([c.tpm for c in candidates]),
        max_parallel_requests=_first([c.max_parallel_requests for c in candidates]),
        budget=budget,
    )
