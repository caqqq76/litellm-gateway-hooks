"""Redis-backed counters. One atomic Lua call to admit a request, one to charge/release it afterwards.

Assumes a single (non-cluster) Redis: the admit script touches user and team keys together.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote

Kind = Literal["rpm", "tpm", "conc", "broll", "bfix"]

ADMIT_LUA = r"""
local now = tonumber(ARGV[1])
local member = ARGV[2]
local n = tonumber(ARGV[3])
-- dry = '1': only report whether the entries would admit a request at `now` (which may be in the future).
-- Nothing is written or pruned. Only budget kinds ('broll', 'bfix') may be sent in dry mode.
local dry = ARGV[4] == '1'

local function rolling(key, window, bucket)
  local cutoff = now - window
  local flat = redis.call('HGETALL', key)
  local buckets = {}
  local total = 0
  for i = 1, #flat, 2 do
    local b = tonumber(flat[i])
    local v = tonumber(flat[i + 1])
    if b + bucket <= cutoff then
      if not dry then redis.call('HDEL', key, flat[i]) end
    else
      total = total + v
      buckets[#buckets + 1] = {b, v}
    end
  end
  return total, buckets
end

local worst_idx, worst_wait, worst_cur = 0, -1, '0'
for i = 1, n do
  local key = KEYS[i]
  local base = 4 + (i - 1) * 4
  local kind = ARGV[base + 1]
  local limit = tonumber(ARGV[base + 2])
  local p1 = tonumber(ARGV[base + 3])
  local p2 = tonumber(ARGV[base + 4])
  local wait, cur = -1, 0

  if kind == 'rpm' then
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now - p1)
    cur = redis.call('ZCARD', key)
    if cur >= limit then
      local e = redis.call('ZRANGE', key, cur - limit, cur - limit, 'WITHSCORES')
      wait = tonumber(e[2]) + p1 - now + 1
    end
  elseif kind == 'tpm' or kind == 'broll' then
    local buckets
    cur, buckets = rolling(key, p1, p2)
    if cur >= limit then
      table.sort(buckets, function(a, b) return a[1] < b[1] end)
      local running = cur
      for _, bv in ipairs(buckets) do
        running = running - bv[2]
        if running < limit then
          wait = bv[1] + p2 + p1 - now
          break
        end
      end
    end
  elseif kind == 'bfix' then
    cur = tonumber(redis.call('GET', key) or '0')
    if cur >= limit then
      wait = p1 - now
    end
  elseif kind == 'conc' then
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
    cur = redis.call('ZCARD', key)
    if cur >= limit then
      wait = p2
    end
  end

  if wait >= 0 then
    if wait < 1 then wait = 1 end
    if wait > worst_wait then
      worst_idx, worst_wait, worst_cur = i, wait, tostring(cur)
    end
  end
end

if worst_idx > 0 then
  return {0, worst_idx, math.floor(worst_wait), worst_cur}
end
if dry then
  return {1}
end

for i = 1, n do
  local key = KEYS[i]
  local base = 4 + (i - 1) * 4
  local kind = ARGV[base + 1]
  local p1 = tonumber(ARGV[base + 3])
  if kind == 'rpm' then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, p1)
  elseif kind == 'conc' then
    redis.call('ZADD', key, now + p1, member)
    redis.call('PEXPIRE', key, p1)
  end
end
return {1}
"""

CHARGE_LUA = r"""
local now = tonumber(ARGV[1])
local member = ARGV[2]
local n = tonumber(ARGV[3])
for i = 1, n do
  local key = KEYS[i]
  local base = 3 + (i - 1) * 4
  local kind = ARGV[base + 1]
  local amount = ARGV[base + 2]
  local p1 = tonumber(ARGV[base + 3])
  local p2 = tonumber(ARGV[base + 4])
  if kind == 'tpm' or kind == 'broll' then
    local bucket = now - (now % p2)
    redis.call('HINCRBYFLOAT', key, tostring(bucket), amount)
    redis.call('PEXPIRE', key, p1 + p2)
  elseif kind == 'bfix' then
    redis.call('INCRBYFLOAT', key, amount)
    local ttl = p1 - now
    if ttl < 60000 then ttl = 60000 end
    redis.call('PEXPIRE', key, ttl)
  elseif kind == 'conc' then
    redis.call('ZREM', key, member)
  end
end
return n
"""


@dataclass(frozen=True)
class Entry:
    kind: Kind
    key: str
    limit: float
    p1: int
    p2: int = 0
    # metadata for the 429 response
    scope: Literal["user", "team"] = "user"
    model: str = ""
    window: str | None = None
    period: str | None = None

    @property
    def limit_type(self) -> str:
        return {"rpm": "rpm", "tpm": "tpm", "conc": "concurrency", "broll": "budget", "bfix": "budget"}[self.kind]


@dataclass(frozen=True)
class Blocked:
    entry: Entry
    wait_ms: int
    current: float


class ScriptRunner(Protocol):
    async def admit(self, keys: list[str], args: list[str | int | float]) -> list[object]: ...

    async def charge(self, keys: list[str], args: list[str | int | float]) -> object: ...


class RedisScripts:
    def __init__(self, client) -> None:  # redis.asyncio.Redis (or fakeredis)
        self._admit = client.register_script(ADMIT_LUA)
        self._charge = client.register_script(CHARGE_LUA)

    async def admit(self, keys, args):
        return await self._admit(keys=keys, args=args)

    async def charge(self, keys, args):
        return await self._charge(keys=keys, args=args)


def scope_key(prefix: str, kind: str, scope: str, scope_id: str, model: str, *suffix: str) -> str:
    parts = [prefix, kind, *suffix, scope, quote(scope_id, safe=""), quote(model, safe="")]
    return ":".join(parts)


def _fmt(v: float) -> str:
    return repr(float(v)) if not float(v).is_integer() else str(int(v))


async def admit(runner: ScriptRunner, entries: list[Entry], now_ms: int, member: str) -> Blocked | None:
    """Admit a request at `now_ms`: None and the request is recorded, or the entry that blocks it."""
    return await _run_admit(runner, entries, now_ms, member, dry=False)


async def check_budgets(runner: ScriptRunner, entries: list[Entry], at_ms: int) -> Blocked | None:
    """Would these budget entries admit a request at `at_ms` (a future time is fine)? Writes nothing."""
    if any(e.limit_type != "budget" for e in entries):
        raise ValueError("check_budgets only takes budget entries")
    return await _run_admit(runner, entries, at_ms, "", dry=True)


async def _run_admit(runner: ScriptRunner, entries: list[Entry], now_ms: int, member: str, dry: bool
                     ) -> Blocked | None:
    if not entries:
        return None
    args: list[str | int | float] = [now_ms, member, len(entries), "1" if dry else "0"]
    for e in entries:
        args += [e.kind, _fmt(e.limit), e.p1, e.p2]
    result = await runner.admit([e.key for e in entries], args)
    if int(result[0]) == 1:
        return None
    idx, wait_ms, current = int(result[1]), int(result[2]), result[3]
    if isinstance(current, bytes):
        current = current.decode()
    return Blocked(entry=entries[idx - 1], wait_ms=wait_ms, current=float(current))


@dataclass(frozen=True)
class ChargeItem:
    kind: Kind
    key: str
    amount: float
    p1: int
    p2: int = 0


async def charge(runner: ScriptRunner, items: list[ChargeItem], now_ms: int, member: str) -> None:
    items = [i for i in items if i.kind == "conc" or (i.amount > 0 and math.isfinite(i.amount))]
    if not items:
        return
    args: list[str | int | float] = [now_ms, member, len(items)]
    for i in items:
        args += [i.kind, _fmt(i.amount), i.p1, i.p2]
    await runner.charge([i.key for i in items], args)
