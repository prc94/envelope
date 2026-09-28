# Evaluation model gateway

`gateway.py` is a standard-library-only recording proxy for OpenAI-compatible Chat
Completions, routed to OpenRouter. It is the only process that holds the provider key.
Run it as a dedicated OS user with the key in a `0600` file; the runner and the harnesses
only ever see per-trial tokens.

```sh
python3 gateway.py \
  --listen 127.0.0.1:8787 --listen 172.17.0.1:8787 \
  --admin-socket /run/envelope-gateway/admin.sock \
  --key-file /etc/envelope-gateway/openrouter.key \
  --ledger /var/lib/envelope-gateway/ledger.jsonl \
  [--pins pins.json] [--aliases aliases.json] [--record-bodies]
```

- `--listen` may repeat. Only loopback and private (Docker bridge) addresses are accepted;
  wildcard and public addresses are refused at startup. Reach targets through reverse
  tunnels bound to loopback on the target.
- The key file must not be readable by group or others, or the gateway refuses to start.
- The admin socket is created `0660`: give it to the gateway user and the runner's group.
- `--pins` is `{model: [provider, ...]}`; matching requests get
  `provider: {order, allow_fallbacks: false}`. `--aliases` is `{requested: served}` for
  models the upstream reports under another name. Both can also be set per token.
- `--record-bodies` adds request and response bodies to the ledger, with the key and all
  trial tokens replaced by `[redacted]`. Credentials and `Authorization` headers are never
  recorded otherwise.
- Only `POST /v1/chat/completions` is routed. `/v1/messages` and `/v1/responses` are
  rejected with an error naming the family (`anthropic_messages`, `openai_responses`).

## Deployment (unprivileged LXC)

The gateway runs in its own container so the runner cannot read the provider key.
`eval/gateway/deploy-lxc.sh` (run from the runner host, needs root ssh to the Proxmox host;
idempotent) creates container `envelope-gateway` (Debian 13, unprivileged, `onboot`) from a
template that must already be on the host, installs `gateway.py` and a hardened systemd
unit, streams the OpenRouter key from `~/.qwen/settings.json` (`env.OPENROUTER_API_KEY`)
into a `0600` file readable only by the gateway user, and creates the `gw-runner` account.

`gw-runner` can log in only with the runner's key (`~/.config/envelope-eval/gateway_ed25519`),
which is restricted to forwarding `127.0.0.1:8787` and to the forced command
`gateway.py relay`, a byte pipe to the admin socket. Nothing else runs and the key file is
not readable. On the runner host the script writes `~/.config/envelope-eval/gateway.toml`
(host, user, key, pinned `known_hosts`) and a user unit `envelope-gateway-tunnel` that
publishes the gateway on `127.0.0.1:8787` here. Use it from Python with
`envelope_eval.runner.gateway.client_from_config()`.

Operate it from the Proxmox host: `pct exec 114 -- journalctl -u envelope-gateway`,
`pct exec 114 -- systemctl restart envelope-gateway`. The container gets its address by
DHCP; pin the lease if the name `envelope-gateway.lab.lan` does not resolve.
The key also remains in the Qwen settings file; rotate it or remove it there once the
gateway works (design E15).

## App attribution

OpenRouter's app column and rankings come from request headers, and the gateway builds the upstream request itself, so headers a
harness sets (Qwen Code's `customHeaders`) never arrive. Give the gateway `--app-url` (sent as `HTTP-Referer`, the identifier; without
it no app is created), `--app-title` (`X-OpenRouter-Title`) and optionally `--app-categories` (`X-OpenRouter-Categories`, for example
`cli-agent`); they are sent on every upstream call, including the model-list one, and a harness cannot override them.

## Transient upstream errors

Rate limits (429), 500/502/503/504 and connections that fail before a response are retried
inside the gateway with exponential backoff (`--retry-base` 2 s doubling up to `--retry-cap`
45 s, honouring `Retry-After`), for at most `--upstream-retries` (8) extra attempts and
`--retry-budget` (150 s) of waiting per request, so the harness sees the error only if it
persisted. The last response is relayed unchanged when the limits run out. Client errors and
guardrail rejections are never retried. The ledger records `attempts`, `retried` (the absorbed
statuses) and `retry_wait`. (The provider's shared pool rate-limits `qwen/qwen3.8-flash`, and
Qwen Code's own backoff turned that into 15 requests and two minutes per trial.)

## Admin protocol

One JSON object per line over the Unix socket (`python3 gateway.py relay --socket PATH` pipes
stdin/stdout to it), one reply line per request:

```json
{"op":"issue","lane":"linux","job":"job-1","trial":"t-1","allowed_models":["model"],
 "request_cap":100,"auxiliary_models":["small"],"aliases":{},"pins":{}}
{"op":"state","token":"..."}
{"op":"revoke","token":"..."}
{"op":"ledger","trial":"t-1"}
{"op":"models","check":["model-a","model-b"],"refresh":false}
{"op":"scan_tar"}   (a tar stream of the directory follows; the client half-closes to finish)
```

- `issue` returns `{token, lane, job, trial}`. `auxiliary_models` are also allowed models.
  `request_cap` counts main requests only.
- `state` returns `in_flight`, `idle_seconds` (in flight: since the last content, or the
  request start; otherwise since the last request finished), request counts and `flags`
  (`disallowed_model`, `model_mismatch`, `runaway`).
- `models` returns the models the provider key may use (`GET /api/v1/models/user`, which the
  provider filters by the key's guardrails; cached for ten minutes), and with `check` splits
  a list into `allowed` and `blocked`. `issue` uses the same list: it refuses a token for a
  blocked model (`model blocked by the key's guardrail: ...`) and reports `model_check`
  `"ok"` or `"unavailable"` (list not obtainable: nothing is blocked on that basis).
  Provider errors that say the guardrail excluded every endpoint are recorded as
  `guardrail_blocked`, flag the trial and drop the cached list.
- `ledger` returns the trial's records (the ledger lives on the gateway host).
- `scan_tar` reports `{file, offset}` for every occurrence of the provider key in the
  streamed tar without extracting it or revealing the key. The runner fails the job on any
  finding. (`scan` with a path on the gateway host also exists, for local use.)

## Ledger

One JSONL record per request (and per rejection), flushed and fsynced. Fields: timestamp,
token identity, API family, `role` (`main` or `auxiliary`: a declared auxiliary model, or a
request without tools once the trial has made requests with tools), requested and served
model, provider, `model_mismatch` (`null` when the upstream reports no model), sampling
parameters, tools/system/prompt hashes and the offered tool names, completion, tool calls, finish reason, usage
(prompt, completion, cached, reasoning tokens, cost, or `{"unknown": reason}`), time to
first and last content (keep-alive comments do not count), duration, `attempts`, `retried`, `retry_wait`, end reason
(`complete`, `stream_error`, `truncated_stream`, `guardrail_blocked`, `http_<status>`, or an exception name) and
error.

## Tests

`scripts/dev.sh test eval/tests/test_gateway_http.py` runs everything against a local fake
upstream; no network or key is needed.
