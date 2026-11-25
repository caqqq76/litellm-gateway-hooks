# Calling the LLM proxy: user identity and rate-limit handling

The proxy enforces limits per user and per model: requests per minute, tokens per minute, concurrent requests,
and spend budgets (15 minutes, 1 hour, 1 day, with separate peak and off-peak budgets, where peak is
Taiwan working hours 09:00 to 18:00). A request over a limit is rejected with HTTP 429 and tells you exactly
how long to wait. A reference client is in `app_example/llm_client.py`, so copy it rather than rewriting it.

## 1. Send the username on every request

Shared application keys: put the username (the NT account) in the `x-end-user-id` header on every request.
Personal API keys (a key that belongs to one user): the header is optional, the proxy uses the key's owner.
A request with neither gets HTTP 400. Both kinds of traffic from the same person share one set of limits.

```python
await litellm.acompletion(..., extra_headers={"x-end-user-id": username})
```

Do not also set the OpenAI `user` field or the `x-litellm-end-user-id` / `x-litellm-customer-id` headers to a
different value. The proxy reads those too, and the standard `x-litellm-*` headers take priority.

## 2. Turn off the SDK's automatic retries

Pass `max_retries=0` to `litellm.acompletion` / `litellm.completion`, leave `num_retries` unset, and don't set
`litellm.num_retries` globally (with a `litellm.Router`, set `num_retries=0`). The SDK's own retries ignore waits
longer than 60 seconds, so for budgets they would retry seconds later and fail again.

## 3. Read the 429 response

Catch `litellm.RateLimitError`. The headers are on `e.litellm_response_headers` (fall back to
`e.response.headers`). Lookups are case-insensitive.

| Header | Meaning |
|---|---|
| `retry-after-ms` | Exact wait in milliseconds. Prefer this one |
| `retry-after` | The same wait in whole seconds, rounded up |
| `x-ratelimit-limit-type` | `rpm`, `tpm`, `concurrency` or `budget` |
| `x-ratelimit-scope` | `user`, or `team` for a shared team budget |
| `x-ratelimit-model` | Model whose limit was hit |
| `x-ratelimit-limit` | The limit (USD for budgets) |
| `x-ratelimit-reset-at` | When the request will succeed, ISO-8601 in Taipei time |
| `x-ratelimit-window` | `15m`, `1h` or `1d` (budgets only) |
| `x-ratelimit-period` | `peak` or `off_peak` (budgets only) |

## 4. Retry policy

- Wait of 60 seconds or less (normally `rpm`, `tpm`, `concurrency`): sleep for the wait plus 0 to 500 ms of
  random jitter, then retry. At most 3 attempts in total.
- Wait over 60 seconds (normally `budget`): don't retry. Show the user something like "Your budget for
  gpt-4o is used up. It resets at 18:00." using `x-ratelimit-reset-at`.
- 429 without `retry-after`: back off 1 s, 2 s, 4 s, at most 3 attempts.
- HTTP 403 means the model isn't allowed for this user (or is switched off). Don't retry.

A peak-hour budget never blocks past 18:00. Off-peak has its own budget, so a user who runs out during the day
can usually continue after 18:00. Some expensive models may be configured as off-peak only. They return 429
with a reset time of 18:00 during working hours.

## 5. Streaming

Limits are checked before the first chunk, so a 429 is raised by the `acompletion(..., stream=True)` call
itself, never in the middle of a stream. Retry only if no chunk was received. Never retry after partial output.

## 6. If the limit service is down

The proxy lets requests through when its limit store is unavailable, so don't treat a missing limit header on
a successful response as an error.
