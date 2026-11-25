# litellm-gateway-hooks

Custom hook code that extends the LiteLLM proxy (written and tested against LiteLLM v1.108.4). It decides who a
request belongs to, applies team and user model routing, and enforces per-user and per-team rate limits and
peak/off-peak budgets. More routing decisions are planned (see [Model routing](#model-routing)).

The repository is organised by **LiteLLM hook point**. LiteLLM loads one object, `GatewayHooks`
(`gateway_hooks/gateway.py`). Each of its methods is a hook point, and each hook point has its own module in
`gateway_hooks/hooks/`. Those modules call the feature code in `core/`, `routing/` and `limits/`.

## Repository layout

```
gateway_hooks/
  callback.py              what LiteLLM loads: gateway_hooks.callback.proxy_handler_instance
  gateway.py               GatewayHooks(CustomLogger): one method per hook point, fail-open wrapper
  context.py               services shared by all hook points
  hooks/                   one module per LiteLLM hook point
    pre_call.py              async_pre_call_hook        identify user -> route model -> admit against limits
    log_success_event.py     async_log_success_event    charge actual cost and tokens, free concurrency slot
    post_call_failure.py     async_post_call_failure_hook   free concurrency slot, charge nothing
  core/                    shared by every hook point
    config.py                gateway-hooks.yaml schema and the defaults -> team -> user merge
    identity.py              header / API-key owner -> LiteLLM user_id and team (Postgres, cached)
    calendar.py              Taiwan peak / off-peak calendar, fixed-window boundaries
  routing/
    model_routes.py          team/user model_routes aliases and allowed_models
  limits/
    engine.py                plan, admit, charge, release; 429 responses
    store.py                 Redis Lua scripts
  __main__.py              gateway-hooks-check: validate the config, show resolved limits
gateway-hooks.yaml         annotated example config
tests/                     mirrors the package: core/, routing/, limits/, test_gateway.py
e2e/                       config for an end-to-end run against a real proxy
scripts/                   offline bundle build (build_bundle.sh, install.sh.in)
APP_ENGINEER_GUIDE.md, app_example/   guide and reference client for applications calling the proxy
docs/RUNBOOK.md            step-by-step production setup on an air-gapped proxy, with checks and troubleshooting
```

### Hook points this repo implements

| Hook point | Module | What it does here |
| --- | --- | --- |
| `async_pre_call_hook` | `hooks/pre_call.py` | Identify the user, rewrite `data["model"]` from `model_routes`, reject disallowed models (403), admit against RPM, TPM, concurrency and budgets or reject (429) |
| `async_log_success_event` | `hooks/log_success_event.py` | Charge `response_cost` and `total_tokens`, release the concurrency slot |
| `async_post_call_failure_hook` | `hooks/post_call_failure.py` | Release the concurrency slot |

## Request flow in the proxy

The order in which LiteLLM 1.108.4 calls the hook points for one `/chat/completions` request:

1. **Auth.** `general_settings.custom_auth` if set, otherwise virtual-key auth. Model access for the key, team and
   customer is checked here against the model name the client sent.
2. **`async_pre_call_hook`** on every callback, in `litellm_settings.callbacks` order, before LiteLLM's own limiter.
   Also `CustomGuardrail`s in `pre_call` mode.
3. **`async_moderation_hook`** (and `during_call` guardrails) starts, running concurrently with steps 4 to 6.
4. **Router.** Routing plugins (`router_settings.plugins`), then `async_filter_deployments`, then the routing
   strategy picks a deployment, then `async_pre_call_check`.
5. **`async_pre_call_deployment_hook`**, then the provider call. Per attempt, so again on retries and fallbacks.
6. **`async_post_call_success_deployment_hook`** or **`async_post_call_failure_deployment_hook`**, per attempt.
7. **Response.** Non-streaming: `async_post_call_success_hook`. Streaming: `async_post_call_streaming_iterator_hook`
   wraps the stream, and `async_post_call_streaming_hook` sees each chunk. `async_post_call_response_headers_hook`
   adds headers.
8. **Failure** anywhere from step 2: `async_post_call_failure_hook` (can replace the error), then the error response.
9. **Logging,** after the response (for streams, after the last chunk): `async_log_success_event` or
   `async_log_failure_event`.

## LiteLLM hook points (v1.108.4)

All `CustomLogger` hook points are methods on a subclass of `litellm.integrations.custom_logger.CustomLogger`
registered in `litellm_settings.callbacks`. Signatures below are as defined in v1.108.4; `user_api_key_dict` is
a `UserAPIKeyAuth`.

**Define it on the registered class itself.** The proxy only calls `async_pre_call_hook`,
`async_post_call_response_headers_hook` and `async_post_call_streaming_iterator_hook` if the method is in the
registered class's own `__dict__`. An inherited override is silently skipped (`litellm/proxy/utils.py`,
`callback_capabilities`). The other hooks also work when inherited. This repo defines every hook point on
`GatewayHooks` for that reason.

### Proxy request hooks (proxy only)

| Hook point | Interface | Runs | Can |
| --- | --- | --- | --- |
| `async_pre_call_hook` | `(self, user_api_key_dict, cache: DualCache, data: dict, call_type) -> dict \| str \| Exception \| None` | After auth, before routing | Modify `data` (return it, including `data["model"]`). Reject by raising (`HTTPException` keeps its status and headers). A returned string becomes a 200 reply with that text as the assistant message on `/chat/completions` and `/completions`, and a 400 elsewhere |
| `async_moderation_hook` | `(self, data: dict, user_api_key_dict, call_type) -> Any` | Concurrently with the LLM call | Reject by raising (the LLM result is discarded). Return value ignored |
| `async_post_call_success_hook` | `(self, data: dict, user_api_key_dict, response) -> Any` | Non-streaming success, before the response is sent | Replace the response (return a new one) or reject by raising |
| `async_post_call_streaming_hook` | `(self, user_api_key_dict, response: str) -> Any` | Per streamed chunk | Replace the chunk (return a value) |
| `async_post_call_streaming_iterator_hook` | `(self, user_api_key_dict, response: AsyncIterator, request_data: dict) -> AsyncGenerator` | Wraps the whole stream | Yield modified, dropped or extra chunks |
| `async_post_call_failure_hook` | `(self, request_data: dict, original_exception: Exception, user_api_key_dict, traceback_str: str \| None = None) -> HTTPException \| None` | Any failure after auth, including rejections by other pre-call hooks and auth errors | Return or raise an `HTTPException` to replace the client's error |
| `async_post_call_response_headers_hook` | `(self, data: dict, user_api_key_dict, response, request_headers: dict \| None = None, litellm_call_info: dict \| None = None) -> dict[str, str] \| None` | Success (response set) and failure (`response=None`) | Add response headers |
| `async_filter_listed_models` | `(self, user_api_key_dict, model_names: Sequence[str]) -> Sequence[str]` | `/v1/models`, `/model/info`, `/model_group/info` | Hide models from listings (only narrows) |
| `async_post_mcp_tool_call_hook` | `(self, kwargs, response_obj: MCPPostCallResponseObject, start_time, end_time) -> MCPPostCallResponseObject \| None` | After an MCP tool call | Modify or replace the tool result |

### Router and deployment hooks (SDK `Router` and proxy)

| Hook point | Interface | Runs | Can |
| --- | --- | --- | --- |
| `async_filter_deployments` | `(self, model: str, healthy_deployments: list, messages, request_kwargs: dict \| None = None, parent_otel_span=None) -> list[dict]` | After health and cooldown filtering, before the strategy picks | Narrow the candidate deployments of the model group. An exception fails the request |
| `async_pre_call_check` / `pre_call_check` | `(self, deployment: dict, parent_otel_span) -> dict \| None` | After a deployment is picked | Raise `litellm.RateLimitError` to cool the deployment down and try another |
| `async_pre_call_deployment_hook` | `(self, kwargs: dict, call_type) -> dict \| None` | Per attempt, after the deployment is chosen | Return modified kwargs sent to that deployment |
| `async_post_call_success_deployment_hook` | `(self, request_data: dict, response, call_type) -> response \| None` | Per successful attempt | Replace the response |
| `async_post_call_failure_deployment_hook` | `(self, request_data: Mapping, exception: Exception, call_type, fallback_depth: int \| None = None) -> None` | Per failed attempt, including retries and fallback hops | Observe only |
| `async_post_call_streaming_deployment_hook` | `(self, request_data: dict, response_chunk, call_type) -> chunk \| None` | Per chunk from the deployment | Replace the chunk |
| `log_model_group_rate_limit_error`, `log_success_fallback_event`, `log_failure_fallback_event` | `(self, exception \| original_model_group, ...)`, see `custom_logger.py` | On model-group 429s and fallbacks | Observe only |
| `async_pre_routing_hook` | `(self, model, request_kwargs, messages=None, input=None, specific_deployment=False) -> PreRoutingHookResponse \| None` | **Not called for callbacks in v1.108.4.** The router only calls its own strategies (auto-router, complexity router) | Nothing. Use a routing plugin or `async_pre_call_hook` instead |

### Logging hooks (SDK and proxy)

| Hook point | Interface | Runs |
| --- | --- | --- |
| `async_log_pre_api_call` | `(self, model, messages, kwargs)` | Before the provider call |
| `async_log_success_event` | `(self, kwargs, response_obj, start_time, end_time)` | After success. `kwargs["standard_logging_object"]` has `response_cost`, tokens, `end_user`, `model_group`. For streams, once after the last chunk. Once per request, not per attempt |
| `async_log_failure_event` | `(self, kwargs, response_obj, start_time, end_time)` | After failure, once per request |
| `async_log_stream_event` | `(self, kwargs, response_obj, start_time, end_time)` | Per stream event |
| `async_logging_hook` / `logging_hook` | `(self, kwargs: dict, result, call_type) -> (kwargs, result)` | Before loggers see the data. Mask or redact what is logged |
| `async_log_audit_log_event` | `(self, audit_log: StandardAuditLogPayload)` | When an admin audit log entry is written |
| `async_pre_request_hook` | `(self, model: str, messages: list, kwargs: dict) -> dict \| None` | Only on the Anthropic `/v1/messages` pass-through path in v1.108.4 |
| `async_get_chat_completion_prompt` | `(self, model, messages, non_default_params, prompt_id, prompt_variables, dynamic_callback_params, litellm_logging_obj, ...) -> (model, messages, params)` | Prompt management: resolve a `prompt_id` to messages |

### Other extension points

| Extension point | Interface | Configured with | Notes |
| --- | --- | --- | --- |
| Routing plugin | `class P: async def run(self, context: RoutingContext) -> RoutingContext` | `router_settings.plugins: [module.instance]` | Runs before the routing decision for every request. Narrow `context.candidate_models` (deployment `litellm_params.model` values) and add `context.signals`. `context.metadata` carries the request metadata, including `user_api_key_*` fields. Narrowing to nothing fails the request |
| Custom auth | `async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth \| str` | `general_settings.custom_auth: module.fn` | Replaces virtual-key auth. Raise `ProxyException` (not `HTTPException`) to keep response headers. Common checks run only with `custom_auth_run_common_checks: true` |
| Custom guardrail | `CustomGuardrail` subclass with `apply_guardrail(self, inputs, request_data, input_type, logging_obj=None)` | `guardrails:` list, `mode: pre_call \| during_call \| post_call \| ...` | For content checks. Runs in the same pre/during/post phases as the hooks above |
| Custom routing strategy | `CustomRoutingStrategyBase` with `async_get_available_deployment(model, messages, input, specific_deployment, request_kwargs)` | `Router.set_custom_routing_strategy(...)` in Python | SDK `Router` only. There is no proxy config setting for it in v1.108.4 |

## Model routing

Where a routing decision can live, depending on what it needs to change:

| Decision | Hook point | Status |
| --- | --- | --- |
| Map an alias to a model group per team or user, and restrict models | `async_pre_call_hook` (`routing/model_routes.py`) | Implemented |
| Pick another model group from request content, user or time of day | `async_pre_call_hook` (rewrite `data["model"]`) | Add to `routing/`, call from `hooks/pre_call.py` before limits |
| Choose among deployments inside a model group | Routing plugin (`router_settings.plugins`) or `async_filter_deployments` | Add a module in `routing/` and a hook point or plugin entry |
| Change parameters for the chosen deployment | `async_pre_call_deployment_hook` | Add when needed |

Rewriting `data["model"]` happens after auth, so LiteLLM checks the name the client sent against key, team and
customer model lists. The rewritten name must be enforced by `allowed_models`. Limits are counted against the
model after `async_pre_call_hook` routing. Deployment-level decisions made later in the router do not change
that model name.

## Adding a hook point

1. Create `gateway_hooks/hooks/<hook_point>.py` with `async def handle(ctx: Context, ...)`.
2. Add the LiteLLM method to `GatewayHooks` in `gateway.py` with the exact signature above. Wrap it so an
   unexpected error is logged and the request continues (see `async_pre_call_hook`).
3. Put the feature logic in `core/`, `routing/` or `limits/`, not in the hook module.
4. Add tests under `tests/` mirroring the package path.

## Limits and budgets

Per user and per model (after routing):

- RPM and TPM over rolling windows
- concurrent requests
- spend budgets for 15m (rolling), 1h and 1d (fixed), with separate peak and off-peak pools. Peak is
  Mon to Fri 09:00 to 18:00 Asia/Taipei, minus `holidays`, plus `extra_workdays`
- team defaults that users override field by field, and an optional team-wide pool

A 429 carries the exact wait (`retry-after`, `retry-after-ms`, `x-ratelimit-*`). Everything fails open: Redis
or database errors, and unexpected errors in this code, let the request through.

### Who a request belongs to

1. End-user header present: matched against `LiteLLM_UserTable` columns in `user_lookup_fields` order (default
   `user_id`, then `sso_user_id`).
2. No header: the user the API key is bound to (`key_user_fallback`), unless it is in `ignored_key_users`
   (default `default_user_id`, the master key).
3. Neither: `missing_user` (`deny` returns 400).

Counters use the LiteLLM `user_id`, so header traffic and personal-key traffic of one person share limits. The
team is the user's first entry in `LiteLLM_UserTable.teams`, cached for `team_cache_ttl_seconds`.

### Behaviour to be aware of

- **Check-then-charge can overshoot.** Requests already in flight when a budget is reached all complete, so
  overspend is at most roughly `max_parallel_requests` times the cost of one request. The same applies to TPM.
- **Cancelled streams.** When a client disconnects mid-stream, LiteLLM fires neither callback, so the
  concurrency slot comes back after `lease_ttl_seconds` (900). The spend of a cancelled stream is only charged
  if LiteLLM reports it.
- **Rolling windows use buckets.** The 15m budget sums 1-minute buckets and TPM sums 5-second buckets, so a
  charge can count for up to one bucket longer than exact (it errs towards stricter).
- **Fixed windows** are clock-aligned: 1h on the hour, 1d on the Taipei calendar day. A window can instead start
  with the peak/off-peak period (`anchor: period_start`).
- **`0` means off.** `0` for rpm/tpm/concurrency returns 403. `0` for a budget returns 429 until the period
  changes ("off-peak only" models). `-1` lifts a limit that a lower layer set.
- **RPM counts admitted requests**, including ones that fail later.
- **Identity is trusted.** The header is not verified. Any holder of an API key can name any user, including
  through the always-on `x-litellm-end-user-id` header or the body `user` field.
- **Database outage.** A header value that can't be looked up is counted under the raw header value instead of
  the user_id, and team settings are skipped until the database answers again. Lookups pause 5 s after a failure.
- **One Redis, one proxy instance.** The scripts touch user and team keys together, so Redis Cluster isn't
  supported. Time comes from the proxy host's clock.
- **Holiday lists** are manual. Update `holidays` / `extra_workdays` from the DGPA calendar every year.

## Install

For a production rollout, follow [docs/RUNBOOK.md](docs/RUNBOOK.md): database pre-checks, install, config, smoke
test, troubleshooting and rollback. The short version follows.

Offline (air-gapped) install from the bundle, into the same Python environment as the LiteLLM proxy:

```bash
tar xzf litellm-gateway-hooks-<VERSION>-bundle.tar.gz
cd litellm-gateway-hooks-<VERSION>-bundle
sha256sum -c SHA256SUMS
./install.sh /path/to/proxy/python   # e.g. /usr/local/bin/python or /app/.venv/bin/python
```

`install.sh` runs `pip install --no-index --find-links wheelhouse litellm-gateway-hooks`, so it never touches
the network. LiteLLM 1.108.x must already be installed in that environment (it is not bundled).

The same bundle is also published as a Docker image (`Dockerfile`, built with `docker build -t
litellm-gateway-hooks:<VERSION> .`), with the bundle in `/opt/litellm-gateway-hooks`. To install it into a proxy
image:

```dockerfile
COPY --from=unviable9201/litellm-gateway-hooks:<VERSION> /opt/litellm-gateway-hooks /tmp/gateway-hooks
RUN /tmp/gateway-hooks/install.sh /path/to/proxy/python && rm -rf /tmp/gateway-hooks
```

Environment for the proxy:

```bash
export GATEWAY_HOOKS_CONFIG=/etc/litellm/gateway-hooks.yaml
export GATEWAY_HOOKS_REDIS_URL=redis://redis:6379/0
```

Proxy `config.yaml`:

```yaml
litellm_settings:
  callbacks: gateway_hooks.callback.proxy_handler_instance   # keep it first in the list

general_settings:
  user_header_mappings:
    - header_name: x-end-user-id      # the header your apps send
      litellm_user_role: customer
```

Validate the config before restarting the proxy (exits non-zero if invalid):

```bash
gateway-hooks-check /etc/litellm/gateway-hooks.yaml
gateway-hooks-check /etc/litellm/gateway-hooks.yaml --user alice.chen --team engineering --model chat-default
```

Restart the proxy after changing the config. Don't also configure LiteLLM's built-in end-user (customer)
budgets or RPM/TPM for these users, or both will enforce.

## Development

```bash
scripts/install-git-hooks.sh         # once per clone: block commits and pushes that contain secrets
uv venv .venv && uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest -q        # unit tests, fakeredis runs the real Lua scripts
scripts/build_bundle.sh              # build/litellm-gateway-hooks-<version>-bundle.tar.gz (needs network)
```

The git hooks (`.githooks/`) run gitleaks with `.gitleaks.toml`: `pre-commit` scans the staged changes and
`pre-push` scans every commit being pushed, so a commit made with `--no-verify` is still caught at push. Both
block when gitleaks is missing. `git push --no-verify` skips them, so also turn on GitHub secret scanning push
protection for the repository.

The end-to-end run (`e2e/`) uses a real LiteLLM 1.108.4 proxy, a fakeredis TCP server and mocked model
responses. The user lookup against a real Postgres has not been exercised. Its queries follow LiteLLM's
`schema.prisma`, so test it against your database first
([runbook, team lookup check](docs/RUNBOOK.md#team-lookup-check-required-never-tested-before-production)).
