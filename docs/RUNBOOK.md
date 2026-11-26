# Production setup: litellm-gateway-hooks for LiteLLM (air-gapped)

## Purpose and rules for the agent

This runbook installs litellm-gateway-hooks 0.0.1 into an existing, air-gapped LiteLLM 1.108.x proxy. It adds per-user, per-model RPM, TPM, concurrency and spend budgets (15m rolling, 1h and 1d fixed, separate peak and off-peak pools), and returns HTTP 429 with an exact retry time.

Rules for an AI agent assisting with this setup:

1. Follow the steps in order. Each step ends with a check. Do not continue until the check passes.
2. Never download anything on the proxy host. Everything needed is in the bundle. Never use `pip install` without `--no-index`.
3. Do not invent values (limits, team names, header names, holiday dates). If a value is missing, ask the operator.
4. Never print or log secrets (master key, Redis password, database URL).
5. Do not edit files inside the installed `gateway_hooks` package. All behaviour is controlled by the config file `gateway-hooks.yaml` and environment variables.
6. If a check fails, use the Troubleshooting table. If no row matches, stop and report the exact command and output.

### Fixed facts

| Item | Value |
| --- | --- |
| Version | 0.0.1 |
| Bundle file | `litellm-gateway-hooks-0.0.1-bundle.tar.gz` |
| Bundle SHA-256 | Not fixed: each build of the tarball has its own hash. Record it as `<BUNDLE_SHA256>` (Prerequisites) |
| Bundle as a Docker image | `unviable9201/litellm-gateway-hooks:0.0.1@sha256:c7e3b44cd0a66e8598a90ccb4180f11094979ace82b5bcc56447371c7f30e633`, bundle in `/opt/litellm-gateway-hooks` |
| SHA-256 of the image's `SHA256SUMS` | `90112a6c9f5d4512d830ef7e998d3955aa2854bd671f448dc8fb8b3b551c2366` |
| Source | <https://github.com/caqqq76/litellm-gateway-hooks> |
| Python package / import name | `litellm-gateway-hooks` / `gateway_hooks` |
| Required LiteLLM version | 1.108.x with the proxy extras (`litellm[proxy]`), already installed, not in the bundle |
| Python | 3.10 or newer, same interpreter as the proxy |
| Callback path for proxy config | `gateway_hooks.callback.proxy_handler_instance` |
| Config check command | `<PY> -m gateway_hooks <CONFIG>` (the same as `gateway-hooks-check <CONFIG>`) |
| Required env vars | `GATEWAY_HOOKS_CONFIG`, `GATEWAY_HOOKS_REDIS_URL` |
| Timezone for peak hours | `Asia/Taipei`, peak Mon to Fri 09:00 to 18:00 |
| Budget windows | `15m` (rolling), `1h` (fixed, clock hour), `1d` (fixed, Taipei calendar day) |
| Behaviour when Redis or DB is down | Fail open: requests are allowed |

## Getting the bundle

The bundle is built on a host with internet access and carried into the air-gapped network. Use one of:

- **Tarball.** In a clone of this repository, run `scripts/build_bundle.sh`. It writes `build/litellm-gateway-hooks-0.0.1-bundle.tar.gz` and prints its SHA-256 on the last line. Send that hash to the operator separately from the file; it is `<BUNDLE_SHA256>`.
- **Docker image.** On the connected host, pull by digest (Docker verifies it) and tag it:
  `docker pull unviable9201/litellm-gateway-hooks:0.0.1@sha256:c7e3b44cd0a66e8598a90ccb4180f11094979ace82b5bcc56447371c7f30e633`, then
  `docker tag unviable9201/litellm-gateway-hooks@sha256:c7e3b44cd0a66e8598a90ccb4180f11094979ace82b5bcc56447371c7f30e633 unviable9201/litellm-gateway-hooks:0.0.1`.
  Save it with `docker save -o litellm-gateway-hooks-0.0.1.image.tar unviable9201/litellm-gateway-hooks:0.0.1`, carry the file over, and run `docker load -i litellm-gateway-hooks-0.0.1.image.tar` on the image build host. Step 1 checks it there.

## Prerequisites

Confirm every item before Step 1. Record the values in the right column. Later steps refer to them by name.

| Check | Command or source | Record as |
| --- | --- | --- |
| LiteLLM 1.108.x installed in the proxy's Python | `<PY> -c "import importlib.metadata as m; print(m.version('litellm'))"` | `<PY>` = path of that Python |
| LiteLLM proxy extras present in that Python | `<PY> -c "import fastapi"` prints nothing |  |
| pip available in that Python | `<PY> -m pip --version` |  |
| Redis reachable from the proxy host (single node, not Cluster) | `redis-cli -u <REDIS_URL> ping` returns `PONG` | `<REDIS_URL>` |
| Proxy uses LiteLLM's Postgres (`DATABASE_URL` set) | proxy environment |  |
| Proxy master key | operator | `<MASTER_KEY>` |
| Name of the HTTP header the apps send with the username | application team | `<USER_HEADER>` (example `x-end-user-id`) |
| Path of the proxy `config.yaml` | operator | `<PROXY_CONFIG>` |
| Where the config file `gateway-hooks.yaml` will live | operator | `<CONFIG>` (example `/etc/litellm/gateway-hooks.yaml`) |
| How the proxy is restarted (systemd, Docker, Kubernetes) | operator |  |
| Limit values per team, per user, per model | owner of the limits |  |
| Taiwan public holidays and make-up workdays (補班) for the year, from the DGPA calendar | 行政院人事行政總處 辦公日曆表 |  |
| Bundle on the proxy host (or image loaded on the image build host) | Getting the bundle | `<BUNDLE_SHA256>` for a tarball |

If the proxy runs in a container, the install must go into the image (build step) or a mounted volume, not a running container that will be replaced.

## Step 0: Database compatibility pre-check

Run these read-only queries against LiteLLM's Postgres before installing. They show whether the current users, teams, keys and customer settings work with the limiter. Do not change the database in this step. Report each result to the operator.

Connect with `psql "$DATABASE_URL"` (the proxy's database). Gather four inputs first:

- `<USER_KEYS>`: every key under `users:` in the planned `gateway-hooks.yaml`
- `<TEAM_KEYS>`: every key under `teams:` (a team\_id or team\_alias)
- `<ROUTE_NAMES>`: every alias in all `model_routes`, plus every model the apps send directly
- `<APP_KEYS>`: the `key_alias` of each API key the applications use

Write lists as SQL literals, for example `('alice','bob')`.

### Team membership

The limiter takes a user's team from `LiteLLM_UserTable.teams`, first entry, and falls back to `team_id` when `teams` is empty.

```sql
-- C1 users in more than one team (expected: 0 rows)
SELECT user_id, teams FROM "LiteLLM_UserTable" WHERE cardinality(teams) > 1;

-- C2 team_id column disagrees with teams[1] (expected: 0 rows)
SELECT user_id, team_id, teams FROM "LiteLLM_UserTable"
WHERE team_id IS NOT NULL AND cardinality(teams) > 0 AND team_id <> teams[1];

-- C3 membership points to a team that does not exist (expected: 0 rows)
SELECT u.user_id, m.tid FROM "LiteLLM_UserTable" u
CROSS JOIN LATERAL unnest(u.teams) AS m(tid)
LEFT JOIN "LiteLLM_TeamTable" t ON t.team_id = m.tid
WHERE t.team_id IS NULL;

-- C4 team member lists disagree with user records (expected: 0 rows)
SELECT t.team_id, m.uid FROM "LiteLLM_TeamTable" t
CROSS JOIN LATERAL unnest(t.members) AS m(uid)
LEFT JOIN "LiteLLM_UserTable" u ON u.user_id = m.uid
WHERE u.user_id IS NULL OR NOT (t.team_id = ANY(u.teams));

-- C5 users with no team: only user and default limits apply (informational)
SELECT user_id FROM "LiteLLM_UserTable" WHERE cardinality(teams) = 0 AND team_id IS NULL;
```

### Config keys exist

```sql
-- C6 each team key matches exactly one team (compare with <TEAM_KEYS>)
SELECT team_id, team_alias FROM "LiteLLM_TeamTable"
WHERE team_id IN <TEAM_KEYS> OR team_alias IN <TEAM_KEYS>;

-- C7 duplicate aliases among those keys (expected: 0 rows)
SELECT team_alias, count(*) FROM "LiteLLM_TeamTable"
WHERE team_alias IN <TEAM_KEYS> GROUP BY team_alias HAVING count(*) > 1;

-- C8 each user key exists as user_id or sso_user_id (compare with <USER_KEYS>)
SELECT user_id, sso_user_id FROM "LiteLLM_UserTable"
WHERE user_id IN <USER_KEYS> OR sso_user_id IN <USER_KEYS>;
```

### Existing LiteLLM limits that would also apply

```sql
-- C9 built-in customer (end-user) settings (expected: 0 rows, or each reviewed)
SELECT e.user_id, e.blocked, e.models, b.max_budget, b.rpm_limit, b.tpm_limit, b.model_max_budget
FROM "LiteLLM_EndUserTable" e
LEFT JOIN "LiteLLM_BudgetTable" b ON b.budget_id = e.budget_id
WHERE e.budget_id IS NOT NULL OR e.blocked OR cardinality(e.models) > 0;

-- C10 the apps' API keys and their teams: model access and app-wide limits
SELECT k.key_alias, k.models AS key_models, k.rpm_limit, k.tpm_limit, k.max_parallel_requests,
       k.max_budget, k.blocked, t.team_alias, t.models AS team_models,
       t.rpm_limit AS team_rpm, t.tpm_limit AS team_tpm, t.max_budget AS team_budget
FROM "LiteLLM_VerificationToken" k
LEFT JOIN "LiteLLM_TeamTable" t ON t.team_id = k.team_id
WHERE k.key_alias IN <APP_KEYS>;
```

Also check the proxy config: `grep -nE 'max_end_user_budget|max_end_user_budget_id' <PROXY_CONFIG>` should return nothing.

### Identity coverage in real traffic

These read the last 7 days of `LiteLLM_SpendLogs`. On a large table run them off-peak. `end_user` is the header value LiteLLM recorded, `api_key` is the hashed key (`LiteLLM_VerificationToken.token`).

```sql
-- C11 header values that match no user by user_id or sso_user_id (expected: 0 rows)
SELECT s.end_user, count(*) AS requests
FROM "LiteLLM_SpendLogs" s
WHERE s."startTime" > now() - interval '7 days' AND coalesce(s.end_user, '') <> ''
  AND NOT EXISTS (SELECT 1 FROM "LiteLLM_UserTable" u
                  WHERE u.user_id = s.end_user OR u.sso_user_id = s.end_user)
GROUP BY s.end_user ORDER BY requests DESC LIMIT 50;

-- C12 how traffic would be identified
SELECT CASE WHEN coalesce(s.end_user, '') <> '' THEN 'header'
            WHEN k.user_id IS NOT NULL AND k.user_id <> 'default_user_id' THEN 'key owner'
            ELSE 'no user' END AS identified_by,
       count(*) AS requests
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" k ON k.token = s.api_key
WHERE s."startTime" > now() - interval '7 days'
GROUP BY 1 ORDER BY 2 DESC;

-- C12b keys behind 'no user' traffic
SELECT coalesce(k.key_alias, k.key_name, '(master key or deleted key)') AS key, count(*) AS requests
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" k ON k.token = s.api_key
WHERE s."startTime" > now() - interval '7 days' AND coalesce(s.end_user, '') = ''
  AND (k.user_id IS NULL OR k.user_id = 'default_user_id')
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- C13 LiteLLM's own user-level limits, which also apply to personal-key traffic (expected: 0 rows, or reviewed)
SELECT user_id, sso_user_id, max_budget, rpm_limit, tpm_limit, max_parallel_requests, model_max_budget
FROM "LiteLLM_UserTable"
WHERE max_budget IS NOT NULL OR rpm_limit IS NOT NULL OR tpm_limit IS NOT NULL
   OR max_parallel_requests IS NOT NULL OR model_max_budget::text NOT IN ('{}', 'null');
```

### How to read the results

| Check | Result | Impact | Action |
| --- | --- | --- | --- |
| C1 | Any row | The limiter uses only the first team | Leave each user in one team, or confirm the first entry is the intended team |
| C2 | Any row | `teams[1]` wins over `team_id` | Confirm `teams[1]` is correct |
| C3, C4 | Any row | Stale membership, so the user may get the wrong or no team settings | Fix membership in the LiteLLM UI or API before go-live |
| C5 | Any row | No team settings for these users | Confirm that is intended |
| C6, C8 | A key missing from the results | That config entry never applies | Fix the key spelling or create the team or user |
| C7 | Any row | Several teams share one alias, and all get the same settings | Key those teams by team\_id instead |
| C9 | `budget_id` set | LiteLLM's own customer budget or limits enforce as well | Remove the customer budget (`/customer/update`) or accept double enforcement |
| C9 | `blocked` true | LiteLLM rejects that user before the limiter | Confirm it is intended |
| C9 | `models` not empty | LiteLLM checks the name the app sends (the alias) against this list | Add every name in `<ROUTE_NAMES>` or empty the list |
| C10 | `key_models` or `team_models` not empty and without `all-proxy-models` | LiteLLM rejects names not listed, during auth, before routing | Add every name in `<ROUTE_NAMES>` to the key (or its team) |
| C10 | `rpm_limit`, `tpm_limit`, `max_parallel_requests` or `max_budget` set | One cap shared by all users of that app. Its 429 always says 60 s | Raise it above total expected traffic, or remove it |
| C10 | `blocked` true | The app cannot call the proxy | Fix before go-live |
| C11 | Any row | These header values get default limits, never their team's | Fix the value the app sends (case, domain prefix such as `CORP\`) or set `sso_user_id`. If the header carries an email, add `user_email` to `user_lookup_fields` |
| C12 | `no user` above 0 | With `missing_user: deny` that traffic gets 400 | Bind those keys to a user (C12b), or set `missing_user: allow` |
| C13 | Any row | LiteLLM enforces these on the user's personal keys in addition to the limiter | Remove them or accept double enforcement |
| grep | Any line | A default customer budget applies to every user | Remove it |

Go to Step 1 only when C1 to C4, C7 and C11 return no rows, C6 and C8 cover every key, `missing_user` is chosen from C12, and every C9, C10 and C13 row has an agreed action.

## Step 1: Verify the bundle

From the tarball:

```bash
sha256sum litellm-gateway-hooks-0.0.1-bundle.tar.gz
tar xzf litellm-gateway-hooks-0.0.1-bundle.tar.gz
cd litellm-gateway-hooks-0.0.1-bundle
sha256sum -c SHA256SUMS
```

Check: the first hash equals `<BUNDLE_SHA256>`, and every line of `sha256sum -c` ends in `OK`. If not, stop: the copy is corrupt or altered.

From the Docker image:

```bash
docker run --rm unviable9201/litellm-gateway-hooks:0.0.1 sha256sum SHA256SUMS
docker run --rm unviable9201/litellm-gateway-hooks:0.0.1 sha256sum -c SHA256SUMS
```

Check: the first hash equals "SHA-256 of the image's `SHA256SUMS`" in Fixed facts, and every line of the second command ends in `OK`. If not, stop. (A registry digest is not always kept by `docker save` and `docker load`, so it is not used here.)

Bundle contents (in the extracted folder, or `/opt/litellm-gateway-hooks` in the image):

| Path | Purpose |
| --- | --- |
| `wheelhouse/` | `litellm_gateway_hooks-0.0.1` wheel and sdist, plus `redis`, `async-timeout`, `tzdata` wheels (pure Python) |
| `install.sh` | Offline installer |
| `SHA256SUMS` | Checksums of every file in the bundle |
| `examples/gateway-hooks.yaml` | Annotated config template |
| `examples/proxy_config.yaml` | Lines to add to the proxy config |
| `README.md` | Package reference |
| `APP_ENGINEER_GUIDE.md`, `app_example/llm_client.py` | For the application team (Step 7) |

## Step 2: Install offline

On a host, from inside the extracted bundle folder:

```bash
./install.sh <PY>
```

The script refuses to run unless LiteLLM 1.108.x is installed for `<PY>`, then runs `<PY> -m pip install --no-index --find-links wheelhouse litellm-gateway-hooks==0.0.1` and imports the package to check it.

Check: the last line reads `litellm-gateway-hooks installed for litellm 1.108.x`. Then confirm:

```bash
<PY> -m pip show litellm-gateway-hooks   # Version: 0.0.1
<PY> -m gateway_hooks --help             # prints usage
```

For a proxy Docker image, install in its Dockerfile, either from the bundle image loaded and checked in Step 1 (by tag, so the build needs no registry):

```dockerfile
COPY --from=unviable9201/litellm-gateway-hooks:0.0.1 /opt/litellm-gateway-hooks /tmp/gateway-hooks
RUN /tmp/gateway-hooks/install.sh /usr/local/bin/python && rm -rf /tmp/gateway-hooks
```

or from the extracted tarball copied into the build context, for example `RUN /app/litellm-gateway-hooks-0.0.1-bundle/install.sh /usr/local/bin/python`. Use the image's own proxy Python for `<PY>`.

## Step 3: Write gateway-hooks.yaml

Copy `examples/gateway-hooks.yaml` to `<CONFIG>` and replace the example values with the real ones. The file is read once at proxy start.

### How a request is matched to a user

1. **Header present** (shared app keys): its value is matched against `LiteLLM_UserTable` columns in `user_lookup_fields` order, default `user_id` then `sso_user_id`. An NT account sent in the header is found through `sso_user_id`.
2. **No header, personal key** (`key_user_fallback: true`): the request belongs to the user the API key is bound to (`LiteLLM_VerificationToken.user_id`). Owners in `ignored_key_users` (default `default_user_id`, the master key) are skipped.
3. **Neither**: `missing_user` applies (`deny` = 400).

All counters use the matched LiteLLM `user_id`. A person's header traffic and personal-key traffic therefore share one set of limits and budgets. The team is that user's `teams[1]` in Postgres.

A header value that matches no user gets `unknown_user` handling (`defaults` or 403) and is counted under the raw header value.

### Rules

- **Keys.** `teams` keys are the LiteLLM `team_id` or `team_alias`. `users` keys are the LiteLLM `user_id` or the `sso_user_id` (NT account), whichever is easier to read.
- **Team membership** comes from LiteLLM's Postgres (`LiteLLM_UserTable.teams`, first entry). It is not set in this file.
- **Model names** are LiteLLM model group names after routing. `"*"` matches any model.
- **Model groups.** `model_groups` lists models that share one set of limits and counters, typically the model groups one model is load-balanced over with `model_routes` (`gpt-4o: [gpt-4o-a, gpt-4o-b]`). Set their limits under the group name. A member cannot have limits of its own (the check rejects it). Ask the operator which models are load-balanced; without a group, a user who sends another member's name directly gets a second set of limits.
- **Resolution**, per field, first match wins: `users.U.models.M`, `users.U.models."*"`, `teams.T.models.M`, `teams.T.models."*"`, `defaults.models.M`, `defaults.models."*"`. A missing field means no limit.
- **Special values.** `-1` removes a limit set by a lower layer. `0` for `rpm`, `tpm` or `max_parallel_requests` blocks the model (403). A `0` budget blocks until the period changes (use it for off-peak-only models).
- **Budgets** are USD per window, set separately for `peak` and `off_peak`. Allowed window names: `15m`, `1h`, `1d`.
- **Routing.** `model_routes` maps an alias the app sends to a model group. `allowed_models` (glob patterns) restricts models after routing. A user's `allowed_models` replaces the team's, and user routes override team routes per alias.
- **Team pool.** `teams.T.team_pool.models` sets limits shared by all members together. It is optional.
- **Holidays.** `peak_hours.holidays` lists weekdays that are not peak. `peak_hours.extra_workdays` lists weekend days that are peak (補班). Update both every year.

### Minimal production example

```yaml
settings:
  redis_url: os.environ/GATEWAY_HOOKS_REDIS_URL
  user_lookup_fields: [user_id, sso_user_id]   # header value matched in this order
  key_user_fallback: true   # no header -> the API key's owner
  ignored_key_users: [default_user_id]
  missing_user: deny        # no header and no key owner -> 400 (set from Step 0, C12)
  unknown_user: defaults    # header value matches no LiteLLM user -> defaults apply

peak_hours:
  timezone: Asia/Taipei
  weekdays: [mon, tue, wed, thu, fri]
  start: "09:00"
  end: "18:00"
  holidays: [2026-01-01]    # fill from the DGPA calendar
  extra_workdays: []

defaults:                   # applies to every user, per model
  models:
    "*":                    # any model not listed below
      rpm: 20
      tpm: 40000
      max_parallel_requests: 2
      budget:
        peak: {"15m": 0.25, "1h": 0.5, "1d": 2.0}
        off_peak: {"15m": 0.5, "1h": 1.0, "1d": 4.0}
    gpt-4o:                 # per-model default: overrides "*" for gpt-4o only
      budget:
        peak: {"1h": 1.0, "1d": 3.0}   # 15m and off_peak still come from "*"
    o1:
      budget:
        peak: {"1d": 0}                # off-peak only
        off_peak: {"1d": 10.0}

teams:
  engineering:
    model_routes: {chat-default: gpt-4o}
    allowed_models: [gpt-4o, gpt-4o-mini]
    models:
      gpt-4o:
        rpm: 30
        budget:
          peak: {"1d": 5.0}

users:
  alice.chen:               # sso_user_id (NT account) or LiteLLM user_id
    models:
      gpt-4o:
        budget:
          peak: {"1d": 15.0}
```

Do not change the `windows` section from the template unless the operator asks. Leave the other `settings` at their template values unless there is a reason (see Reference).

## Step 4: Configure the proxy

Back up `<PROXY_CONFIG>` first: `cp <PROXY_CONFIG> <PROXY_CONFIG>.bak`.

Merge these keys into the existing sections. Do not replace the sections.

```yaml
litellm_settings:
  callbacks:
    - gateway_hooks.callback.proxy_handler_instance   # must be FIRST in the list

general_settings:
  user_header_mappings:
    - header_name: <USER_HEADER>
      litellm_user_role: customer
```

If `callbacks` already exists, insert the line as the first entry and keep the others. If `callbacks` is a single string, turn it into a list.

Set these environment variables for the proxy process (systemd unit, Docker `-e` or Kubernetes `env`):

| Variable | Value |
| --- | --- |
| `GATEWAY_HOOKS_CONFIG` | `<CONFIG>` (absolute path, readable by the proxy user) |
| `GATEWAY_HOOKS_REDIS_URL` | `<REDIS_URL>`, for example `redis://:password@redis:6379/0` |

Do not configure LiteLLM's built-in customer (end-user) budgets, `rpm_limit` or `tpm_limit` for these users. Both would enforce. Key-level and team-level LiteLLM limits may stay.

Check: `grep -n gateway_hooks <PROXY_CONFIG>` shows the callback line, and the environment variables are visible to the proxy process (`cat /proc/<pid>/environ | tr '\0' '\n' | grep GATEWAY_HOOKS` after restart).

## Step 5: Validate and restart

For the smoke test, temporarily add a test user to `<CONFIG>`. Pick a cheap model `<MODEL>` that exists in the proxy:

```yaml
users:
  limiter-smoke-test:
    models:
      <MODEL>: {rpm: 2}
```

Validate, then restart the proxy:

```bash
GATEWAY_HOOKS_REDIS_URL=<REDIS_URL> <PY> -m gateway_hooks <CONFIG>
<PY> -m gateway_hooks <CONFIG> --user limiter-smoke-test --model <MODEL>
```

Check: the first command prints `OK: ...` and the current period (`peak` or `off_peak`) with correct Taipei times, and exits 0. The second shows `rpm=2`. `INVALID: ...` means fix the file and rerun. Restart the proxy and confirm its log shows no `Traceback` and no `gateway_hooks` error.

## Step 6: Smoke test

Run from a host that can reach the proxy. `<PROXY>` is its base URL.

```bash
req() { curl -s -i "<PROXY>/v1/chat/completions" \
  -H "Authorization: Bearer <MASTER_KEY>" -H "Content-Type: application/json" \
  "$@" -d '{"model":"<MODEL>","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}' \
  | grep -iE '^HTTP|^retry-after|^x-ratelimit-'; }

req -H "<USER_HEADER>: limiter-smoke-test"   # 1
req -H "<USER_HEADER>: limiter-smoke-test"   # 2
req -H "<USER_HEADER>: limiter-smoke-test"   # 3
req                                           # 4, no user header
```

| Request | Expected |
| --- | --- |
| 1 and 2 | `HTTP/1.1 200 OK` |
| 3 | `HTTP/1.1 429`, `retry-after-ms` under 60000, `x-ratelimit-limit-type: rpm`, `x-ratelimit-scope: user`, `x-ratelimit-model: <MODEL>` |
| 4 | `HTTP/1.1 400`, body contains `No user:` |

If request 3 returns 200, the limiter is failing open. Check the proxy log for `gateway_hooks: admit failed` (Redis problem).

### Team lookup check (required, never tested before production)

The user and team are read from Postgres. This has never run against a real database before production, so it is required. Use a real person whose `sso_user_id` is `<NT>` (their NT account), whose team `<T>` has a limit in `<CONFIG>`, and who has a personal API key `<PERSONAL_KEY>`.

1. Confirm the record: `SELECT user_id, sso_user_id, teams, team_id FROM "LiteLLM_UserTable" WHERE sso_user_id = '<NT>';`. Note the `user_id` as `<UUID>`. The first entry of `teams` must be `<T>`'s `team_id` (if `<CONFIG>` uses the alias, check it with `SELECT team_id, team_alias FROM "LiteLLM_TeamTable" WHERE team_id = '<team_id>';`).
2. Show the expected limits: `<PY> -m gateway_hooks <CONFIG> --user <NT> --team <T> --model <MODEL>`.
3. **Header traffic.** Send requests with `<USER_HEADER>: <NT>` and the app key until a 429 appears. Its `x-ratelimit-limit` must equal the value from step 2 (a team value, not the default).
4. **Personal-key traffic shares the counters.** Wait for the window to clear. Send one request with `<USER_HEADER>: <NT>`, then requests with `Authorization: Bearer <PERSONAL_KEY>` and no user header. The 429 must come one request earlier than in step 3, because both kinds count against `<UUID>`.
5. Check the proxy log contains no `gateway_hooks: user lookup for <NT> failed`. That warning means the database query failed, and only user and default limits are applied.

When all checks pass, remove `limiter-smoke-test` from `<CONFIG>`, validate again and restart.

## Step 7: Hand off to the application team

Give them `APP_ENGINEER_GUIDE.md` and `app_example/llm_client.py` from the bundle. Replace `x-end-user-id` in both with `<USER_HEADER>` if it differs (`llm_client.py` also reads it from `LLM_END_USER_HEADER`).

The app must:

1. With a shared app key, send `<USER_HEADER>: <NT account>` on every request. With a personal key (bound to one user), the header is optional, and the key's owner is used.
2. Call the LiteLLM SDK with `max_retries=0` and no `num_retries` (also not `litellm.num_retries` globally).
3. On `litellm.RateLimitError`, read `retry-after-ms` from `e.litellm_response_headers`. If the wait is 60 s or less, sleep that long plus up to 0.5 s jitter and retry, at most 3 attempts. If the wait is longer (budgets), do not retry. Show the user `x-ratelimit-reset-at`.
4. Not retry on 403 (model not allowed) or 400 (no user).
5. For streaming, retry only when no chunk was received. A 429 is always raised before the first chunk.

Check: the application team confirms a 429 from their code path is handled per point 3.

## Troubleshooting

Match the exact message first. Log lines from the limiter all start with `gateway_hooks:`.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `install.sh`: `litellm is not installed for <PY>` | Wrong interpreter | Use the Python that runs the proxy (`head -1 $(which litellm)` shows it) |
| `install.sh`: `built for litellm 1.108.x, found ...` | LiteLLM version mismatch | Stop. Needs a bundle built for that LiteLLM version |
| `install.sh`: `ModuleNotFoundError: No module named 'fastapi'` after `Successfully installed` | LiteLLM installed without its proxy extras, so `<PY>` is not the proxy's Python | Use the proxy's Python. Do not install fastapi by hand |
| pip: `No matching distribution found` | Python older than 3.10, or not run from the bundle directory | Check `<PY> --version`. Run `./install.sh` from inside the extracted folder |
| Proxy start: `Set GATEWAY_HOOKS_CONFIG ...` | Env var missing in the proxy process | Set it (Step 4) and restart |
| Proxy start: `Could not import proxy_handler_instance from gateway_hooks.callback` | Package installed into a different Python | Re-run Step 2 with the proxy's `<PY>` |
| Proxy start or check: `unknown windows ['5h']` | Old config file | Remove the window from every budget. Allowed: `15m`, `1h`, `1d` |
| Proxy start or check: other `validation error` | YAML key or type wrong | Fix the named field. Unknown keys are rejected on purpose |
| Log: `admit failed, letting request through: ... is not set` | `GATEWAY_HOOKS_REDIS_URL` missing | Set it and restart |
| Log: `admit failed, letting request through: TimeoutError` or `ConnectionError` | Redis unreachable or slower than `redis_timeout_ms` (50 ms) | Check the network and `redis-cli ping`. Raise `redis_timeout_ms` only if Redis is healthy but remote |
| Log: `CROSSSLOT` | Redis Cluster in use | Use a single Redis node (with a replica if needed) |
| Log: `user lookup for <U> failed` | Postgres unreachable or query slower than `db_timeout_ms` (200 ms). Lookups pause 5 s after a failure | Check `DATABASE_URL` and DB health. Team limits are skipped until it works |
| Log: `unexpected error, letting request through` | A limiter bug. The request was allowed | Report the full traceback that follows as an issue on the repository |
| Header users get default limits, not their team's | Header value matches no `user_id` or `sso_user_id` (case, `DOMAIN\` prefix, email) | Run C11. Fix the value or `sso_user_id`, or add `user_email` to `user_lookup_fields` |
| 400 `No user: send the end-user header or use an API key that belongs to a user` | No header, and the key has no owner (or the owner is in `ignored_key_users`) | Bind the key to a user, send the header, or set `missing_user: allow` (C12) |
| 400 for requests that do send a header | Header name differs from `user_header_mappings` | Make `<USER_HEADER>` in the proxy config match the header the apps send |
| 403 `Model X is not allowed for user Y` | Routed model not in `allowed_models` | Add it to the team or user `allowed_models`, or fix `model_routes` |
| 403 `Model X is disabled for user Y` | A `0` for `rpm`, `tpm` or `max_parallel_requests` applies to that model | Intended to block it. Otherwise change the `0` |
| Never any 429 and no limiter log lines | Callback not loaded | Check `callbacks` in `<PROXY_CONFIG>` and that it is the first entry |
| Budgets never trigger, RPM works | LiteLLM reports cost 0 for that model (no price known) | Add `input_cost_per_token` and `output_cost_per_token` to the model's `litellm_params` in `<PROXY_CONFIG>` |
| Peak or off-peak wrong on a given day | Holiday lists out of date | Fix `holidays` / `extra_workdays`. Confirm with `<PY> -m gateway_hooks <CONFIG>` |
| Concurrency 429 although nothing runs | Client cancelled streams. Slots return after `lease_ttl_seconds` (900 s) | Wait, or lower `lease_ttl_seconds` to above your longest request |

## Changing limits, upgrading, rolling back

**Change limits.** Edit `<CONFIG>`, run `<PY> -m gateway_hooks <CONFIG>` (must print `OK`), then restart the proxy. Counters in Redis survive restarts, so spend already used still counts against the new limits. Moving a user to another team takes effect within 60 s (team cache), with no restart.

**Yearly.** Before 1 January, add next year's `holidays` and `extra_workdays` from the DGPA calendar.

**Upgrade LiteLLM.** Version 0.0.1 is pinned to LiteLLM 1.108.x. Do not upgrade LiteLLM to another minor version until a matching litellm-gateway-hooks build has been tested.

**Upgrade litellm-gateway-hooks.** Get the new bundle (Getting the bundle), repeat Steps 1, 2 and 5, and read the new version's README for config changes.

**Roll back** (limits stop, the proxy works as before):

1. Remove `gateway_hooks.callback.proxy_handler_instance` from `callbacks`, or restore `<PROXY_CONFIG>.bak`.
2. Restart the proxy.
3. Optional: `<PY> -m pip uninstall -y litellm-gateway-hooks`, and delete Redis keys with `redis-cli -u <REDIS_URL> --scan --pattern 'gwh:*' | xargs -r redis-cli -u <REDIS_URL> del`.

Check: requests succeed and no `gateway_hooks` lines appear in the log.

## Reference

### Responses from the limiter

| Status | When | Retry? |
| --- | --- | --- |
| 429 | A rate, concurrency or budget limit is reached | Per `retry-after-ms` (Step 7) |
| 403 | Model not in `allowed_models`, a `0` RPM/TPM/concurrency limit, or an unknown user with `unknown_user: deny` | No |
| 400 | No header and no usable key owner, with `missing_user: deny` | No |

429 headers: `retry-after` (seconds, rounded up), `retry-after-ms`, `x-ratelimit-limit-type` (`rpm`, `tpm`, `concurrency`, `budget`), `x-ratelimit-scope` (`user`, `team`), `x-ratelimit-model`, `x-ratelimit-limit`, `x-ratelimit-reset-at` (ISO-8601, +08:00), and for budgets `x-ratelimit-window` and `x-ratelimit-period`. The same fields are in the JSON body under `error.provider_specific_fields`.

### settings in gateway-hooks.yaml

| Key | Default | Meaning |
| --- | --- | --- |
| `redis_url` | `os.environ/GATEWAY_HOOKS_REDIS_URL` | Redis URL, or `os.environ/<VAR>` |
| `key_prefix` | `gwh` | Prefix of all Redis keys |
| `redis_timeout_ms` | 50 | Slower Redis call = request allowed (fail open) |
| `redis_connect_timeout_ms` | 1000 | Timeout for the first call (connect and script load) |
| `db_timeout_ms` | 200 | Slower user lookup = continue without team settings |
| `team_cache_ttl_seconds` | 60 | How long a user's identity and team are cached |
| `user_lookup_fields` | `[user_id, sso_user_id]` | `LiteLLM_UserTable` columns the header value is matched against, in order (`user_email` also allowed) |
| `key_user_fallback` | `true` | No header: use the API key's owner |
| `ignored_key_users` | `[default_user_id]` | Key owners that are not people |
| `rpm_window_seconds` / `tpm_window_seconds` | 60 / 60 | Rolling RPM and TPM windows |
| `tpm_bucket_seconds` | 5 | TPM bucket size |
| `lease_ttl_seconds` | 900 | A concurrency slot is reclaimed after this |
| `concurrency_retry_after_ms` | 1000 | Wait returned for concurrency 429s |
| `missing_user` | `deny` | No header and no usable key owner: `deny` (400) or `allow` (no limits) |
| `unknown_user` | `defaults` | Header value matches no LiteLLM user: `defaults` or `deny` (403) |

### Redis keys

| Pattern | Type | Holds |
| --- | --- | --- |
| `gwh:rpm:user:<id>:<model>` | sorted set | Request timestamps, last 60 s |
| `gwh:tpm:user:<id>:<model>` | hash | Tokens per 5 s bucket |
| `gwh:conc:user:<id>:<model>` | sorted set | Open requests and their expiry |
| `gwh:b:<period>:15m:user:<id>:<model>` | hash | Spend per 1-minute bucket |
| `gwh:b:<period>:<1h or 1d>:<window start epoch>:user:<id>:<model>` | string | Spend in that fixed window |

Team-pool keys use `team:<team_id>` instead of `user:<id>`. `<model>` is the model group name for models in `model_groups`. `<id>` and `<model>` are URL-encoded. All keys expire on their own.

### Known behaviour

- Check-then-charge: requests in flight when a budget is reached still finish. Overspend is at most about `max_parallel_requests` times the cost of one request. TPM works the same way.
- Spend is LiteLLM's `response_cost` (USD) for the routed model, per user, across all apps and keys. Counters use the LiteLLM `user_id`, so header and personal-key traffic of one person add up.
- During a database outage, header traffic is counted under the raw header value, separately from that person's personal-key traffic, and team settings are skipped.
- Peak and off-peak are separate pools, so a peak budget wait normally ends at 18:00. When the off-peak pool is spent too (or `0`), `retry-after` looks ahead to when a pool really has room, for example to midnight for a spent `1d` off-peak budget. The 429 still names the limit that refused the request.
- RPM counts every admitted request, including ones that fail later.
- The user header is trusted. LiteLLM also reads `x-litellm-end-user-id`, `x-litellm-customer-id` and the body field `user`, and the first two take priority. Any API-key holder can name any user. A header also overrides the owner of a personal key.
- Any unexpected limiter error lets the request through and logs `unexpected error, letting request through`.
- One proxy instance and one Redis node are assumed. Time comes from the proxy host clock (keep NTP on).
