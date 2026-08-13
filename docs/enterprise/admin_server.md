# Admin Server

The Admin Server runs inside your network and acts as the single host that talks to Griptape Cloud on behalf of your Griptape Nodes Application instances. Your instances point at the Admin Server instead of `cloud.griptape.ai` directly, and it forwards each request upstream. This lets you keep individual application instances off the public internet while they continue to license, run sessions, and use Griptape Cloud features.

## Why use it

A studio that deploys Griptape Nodes on-premises usually does not want every application instance reaching the public internet on its own. The Admin Server gives you one place to manage that connection:

- **Lock instances down.** Application instances talk only to the Admin Server, so they never need direct internet egress.
- **One egress point.** Instead of opening internet access for each instance, you allow a single host through your firewall — one rule to manage and one place to audit outbound traffic.
- **Central configuration.** You configure where Griptape Cloud lives and, optionally, which Cloud paths are allowed to leave your network — in one spot rather than per instance.

If your application instances can already reach `cloud.griptape.ai` directly and that is acceptable for your environment, you do not need the Admin Server.

For a diagram of where the Admin Server sits and what traffic flows through it, see [Architecture](../architecture.md#on-premises-configuration).

## Getting the Admin Server

The Admin Server is provided to enterprise customers. [Contact Foundry](https://www.foundry.com/products/griptape/request-demo) to obtain it for your deployment.

## Capabilities

- **Forwards Cloud requests.** It forwards your applications' Griptape Cloud requests to the configured upstream (`https://cloud.griptape.ai` by default) and preserves the caller's `Authorization` header, so licensing and sessions keep working through it. Griptape Cloud remains the authority on authentication and authorization.
- **Validates at startup.** It validates the operator's Griptape Cloud API key once at startup, so a misconfigured deployment fails fast at boot rather than breaking later.
- **Egress filtering.** You can optionally choose exactly which Cloud paths may leave your network (see [`forwarding`](#forwarding)).
- **Health endpoint.** A local `GET /health` endpoint returns `{"status":"ok"}` for liveness and readiness probes.
- **Structured logging.** Each request is logged once at completion (method, path, status, latency, client IP, bytes written) in JSON or text for auditing.

## Configuration

The Admin Server reads its settings from a `config.yaml` file. Configuration is resolved in this order, with later sources overriding earlier ones:

1. Built-in defaults
1. The `config.yaml` file (the default path; it can be pointed elsewhere at startup)
1. Environment variables

!!! note "Which version this describes"

    This page documents Admin Server 0.3.0 and later — run `./server -version` to check which one you have. On earlier versions `read_timeout` and `write_timeout` default to `30s`, which cuts streamed replies off mid-reply; see the [warning below](#server) for what to set.

A complete `config.yaml` with the default values looks like this:

```yaml
server:
  host: "0.0.0.0"
  port: 8080
  read_header_timeout: "10s"
  write_stall_timeout: "60s"
  idle_timeout: "120s"
  shutdown_timeout: "10s"

upstream:
  base_url: "https://cloud.griptape.ai"
  timeout: "120s"
  # Name of the environment variable that holds the Griptape Cloud API key.
  # The key value is never stored here — only the variable name.
  api_key_env: "GT_CLOUD_API_KEY"

logging:
  level: "info"   # debug | info | warn | error
  format: "json"  # json | text

forwarding:
  mode: "allow_all"  # allow_all | allow | deny
  rules: []
```

### server

| Key                   | Default   | Description                                                                                                                                                                    |
| --------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `host`                | `0.0.0.0` | Address the server listens on.                                                                                                                                                 |
| `port`                | `8080`    | Port the server listens on.                                                                                                                                                    |
| `read_header_timeout` | `10s`     | Deadline for a request's headers to arrive (a duration string, e.g. `10s`).                                                                                                    |
| `write_stall_timeout` | `60s`     | How long a single write to a client may block before the connection is dropped. Re-armed before every write, so it does not cap how long a response may take. `0` disables it. |
| `idle_timeout`        | `120s`    | How long an idle keep-alive connection is held open between requests.                                                                                                          |
| `shutdown_timeout`    | `10s`     | Deadline for in-flight requests to finish during a graceful shutdown.                                                                                                          |
| `read_timeout`        | `0` (off) | **Deprecated.** A deadline on reading the *whole* request, so it caps how long an upload may take. Use `read_header_timeout`.                                                  |
| `write_timeout`       | `0` (off) | **Deprecated.** A deadline on writing the *whole* response — any value above `0` cuts streamed responses off mid-reply. Use `write_stall_timeout`.                             |

!!! warning "`write_timeout` breaks streamed responses"

    Earlier versions of this page and of the example config shipped `read_timeout: "30s"` and `write_timeout: "30s"`. If your `config.yaml` contains either line, **set it to `"0"`**:

    ```yaml
    server:
      read_timeout: "0"
      write_timeout: "0"
    ```

    Setting `"0"` is correct on every version. *Deleting* the lines only works on 0.3.0 and later, where the defaults are already `0` — on an earlier build a deleted line falls back to that build's `30s` default and replies keep getting cut off. Once every deployment is on 0.3.0 or later you can drop the lines entirely.

    `write_timeout` is a deadline on the entire response, measured from the moment the request arrives — not an idle timeout. Agent replies stream one token at a time and routinely take longer than 30 seconds, so the server cuts them off mid-reply once the deadline passes. In Griptape Nodes that looks like a chat response that stops mid-sentence, and in the application log like:

    ```text
    httpx.RemoteProtocolError: peer closed connection without sending complete message body
    ```

    `write_stall_timeout` replaces it: it limits how long a single write may block, so a stalled client is still dropped while a healthy stream runs as long as it needs to.

    You do not have to edit the file to test this — the environment variable wins over `config.yaml`:

    ```bash
    export SERVER_WRITE_TIMEOUT=0
    # then restart the Admin Server
    ```

### upstream

| Key           | Default                     | Description                                                                                                                                                                                                                                                                                                                   |
| ------------- | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_url`    | `https://cloud.griptape.ai` | The Griptape Cloud root the server forwards to.                                                                                                                                                                                                                                                                               |
| `timeout`     | `120s`                      | How long to wait for Griptape Cloud to *begin* responding. It never limits how long a response may take, so streamed replies are unaffected. A request that is **not** streamed sends nothing until the whole answer is ready, which is why this default is generous — raise it if long generations return `502 Bad Gateway`. |
| `api_key_env` | `GT_CLOUD_API_KEY`          | The **name** of the environment variable that holds the Griptape Cloud API key.                                                                                                                                                                                                                                               |

The Admin Server requires a Griptape Cloud API key, which it validates at startup to confirm the operator owns a Griptape organization. The key is **not** used on the request path — your applications still send their own `Authorization` header, which is forwarded untouched.

The key value is never stored in `config.yaml`. Instead, `api_key_env` names the environment variable to read it from (default `GT_CLOUD_API_KEY`), and you set the key in the environment:

```bash
export GT_CLOUD_API_KEY="gt-..."
```

To use a different variable name, set `api_key_env` and export the key under that name.

!!! warning "Never commit the API key"

    Keep the API key in the environment, not in `config.yaml`. The config file only names the environment variable to read the key from, so the key itself never needs to live in a file you might check into version control.

### logging

| Key      | Default | Description                                         |
| -------- | ------- | --------------------------------------------------- |
| `level`  | `info`  | Log verbosity: `debug`, `info`, `warn`, or `error`. |
| `format` | `json`  | Log output format: `json` or `text`.                |

The Admin Server writes logs to standard output and standard error — it does not write a log file. If you need one, redirect when you start it (`./server -config config.yaml > admin-server.log 2>&1`), or let your container runtime or service manager collect the streams. Set `format: "text"` if you will be reading the log by eye rather than feeding it to a log collector.

Three lines are worth knowing when diagnosing a request that ended early:

| Line                                        | Meaning                                                                                                                                                                                                                |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `starting server`                           | Lists every timeout actually in effect, with `config.yaml` and environment overrides already resolved. Check this before anything else.                                                                                |
| `response stream aborted before completion` | A response could not be finished. Includes `bytes_out` (how far it got) and `request_id`. Logged as a warning because the usual cause is a user closing a tab mid-reply; the other cause is a write deadline elapsing. |
| `panic recovered`                           | A bug in the Admin Server. The caller received a `500`. Worth reporting.                                                                                                                                               |

### forwarding

The `forwarding` block controls which Cloud paths may egress through the Admin Server. This is an egress control for your network, layered on top of the upstream's own access control.

| Key     | Default     | Description                                                                                     |
| ------- | ----------- | ----------------------------------------------------------------------------------------------- |
| `mode`  | `allow_all` | `allow_all`, `allow`, or `deny` (see below).                                                    |
| `rules` | _(empty)_   | Paths to allow or deny. Each rule is an absolute path; a trailing `/*` makes it a prefix match. |

The three modes:

- **`allow_all`** (default) — forward every path; `rules` is ignored.
- **`deny`** — forward everything **except** matching paths. Best for carving out a few exceptions.
- **`allow`** — forward **only** matching paths. A lockdown posture with an explicit, minimal surface.

Each rule is an absolute path. A trailing `/*` makes it a prefix match (`/api/proxy/*` matches `/api/proxy` and anything under it); otherwise the match is exact. A path that is not permitted is answered locally with `403 {"error":"path not permitted"}` and never reaches the upstream.

For example, to keep the Griptape Cloud model proxy in-network while forwarding everything else:

```yaml
forwarding:
  mode: "deny"
  rules:
    - "/api/proxy/*"
```

For the most restricted posture, use `allow` mode and list only the routes the application needs at runtime. Nothing else egresses. These are the required routes — the Admin Server refuses to start if `allow` mode omits any of them:

```yaml
forwarding:
  mode: "allow"
  rules:
    - "/api/sessions/*"     # session allocation and lifecycle, including /api/sessions/{id}
    - "/api/session-renew"  # keep a session alive
    - "/api/session-release" # end a session
    - "/api/users"          # fetched on startup and on every heartbeat
    - "/api/organizations"  # fetched on startup and on every heartbeat
```

This is the minimal set the product cannot run without. Add further rules only for Cloud features you want to permit (for example, `/api/proxy/*` for the model proxy).

!!! note "Egress control, not authentication"

    Forwarding rules decide which paths may leave your network — they are not authentication; Griptape Cloud remains the authority on who may call what. The core routes the application needs at runtime (session lifecycle, user, and organization routes) can never be blocked: if your configuration would block them, the Admin Server refuses to start and names the offending routes. This means you can tighten egress without accidentally breaking the product.

## Environment variable overrides

Every setting can be overridden with an environment variable, which takes precedence over `config.yaml`:

| Variable                     | Default                     | Description                                                                   |
| ---------------------------- | --------------------------- | ----------------------------------------------------------------------------- |
| `GT_CLOUD_API_KEY`           | _(required)_                | The Griptape Cloud API key (or the variable named by `upstream.api_key_env`). |
| `SERVER_HOST`                | `0.0.0.0`                   | Listen address.                                                               |
| `SERVER_PORT`                | `8080`                      | Listen port.                                                                  |
| `SERVER_READ_HEADER_TIMEOUT` | `10s`                       | Request-header deadline.                                                      |
| `SERVER_WRITE_STALL_TIMEOUT` | `60s`                       | Per-write deadline for a client that stops reading.                           |
| `SERVER_IDLE_TIMEOUT`        | `120s`                      | Idle keep-alive connection deadline.                                          |
| `SERVER_SHUTDOWN_TIMEOUT`    | `10s`                       | Graceful shutdown deadline.                                                   |
| `SERVER_READ_TIMEOUT`        | `0` (off)                   | **Deprecated.** Whole-request read deadline; caps upload duration.            |
| `SERVER_WRITE_TIMEOUT`       | `0` (off)                   | **Deprecated.** Whole-response write deadline; cuts off streamed replies.     |
| `UPSTREAM_BASE_URL`          | `https://cloud.griptape.ai` | Upstream Griptape Cloud root.                                                 |
| `UPSTREAM_TIMEOUT`           | `120s`                      | Upstream response-header timeout.                                             |
| `UPSTREAM_API_KEY_ENV`       | `GT_CLOUD_API_KEY`          | Name of the env var holding the API key.                                      |
| `LOG_LEVEL`                  | `info`                      | Log verbosity.                                                                |
| `LOG_FORMAT`                 | `json`                      | Log output format.                                                            |
| `FORWARDING_MODE`            | `allow_all`                 | `allow_all`, `allow`, or `deny`.                                              |
| `FORWARDING_RULES`           | _(empty)_                   | Comma-separated list of paths to allow or deny.                               |

Duration values must carry a unit — `30s`, `2m` — and `0` disables a timeout. A value with no unit (`SERVER_READ_TIMEOUT=30`) makes the Admin Server refuse to start, rather than quietly running with a different timeout than you asked for.

## Running it

1. Set the Griptape Cloud API key in the environment:

    ```bash
    export GT_CLOUD_API_KEY="gt-..."
    ```

1. Provide a `config.yaml` (see [Configuration](#configuration)) and start the Admin Server. By default it listens on `0.0.0.0:8080` and forwards to `https://cloud.griptape.ai`.

1. Point your Griptape Nodes Application instances at the Admin Server's address instead of `cloud.griptape.ai`.

If the API key is missing or invalid, or the upstream cannot be reached, the Admin Server logs the reason and exits without serving — so configuration problems surface at startup.

## Troubleshooting

- **Chat replies stop mid-sentence**, or the application logs `peer closed connection without sending complete message body`. A streamed reply was cut off. Almost always `write_timeout` is set in your `config.yaml` — see the [warning above](#server). Set it to `"0"` (or `export SERVER_WRITE_TIMEOUT=0`, which overrides the file) and restart.

    The Admin Server's own log confirms it. The `starting server` line reports every timeout it is using, so check `write_timeout` there first; a `response stream aborted before completion` warning shows the server ended the response and how many bytes it had sent. If `write_timeout=0s` and replies are still truncated, the cut is happening somewhere else — check for a load balancer, ingress controller, or TLS terminator in front of the Admin Server, each of which has its own response timeout.

- **`502 Bad Gateway` on a long generation that isn't streamed.** `upstream.timeout` limits how long Griptape Cloud may take to *begin* responding, and a non-streamed generation sends nothing until the whole answer is ready. Raise it.

- **Large uploads fail partway through.** `read_timeout` limits the total time a request body may take to arrive, which caps upload size in practice. It is off by default on 0.3.0 and later; if your `config.yaml` sets it, set it to `"0"`.

- **The server won't start.** Confirm `GT_CLOUD_API_KEY` is set and valid; the Admin Server validates it at startup and fails closed if it cannot. A startup error naming an environment variable (`invalid SERVER_WRITE_TIMEOUT: "30" is not a duration`) means a duration is missing its unit — use `30s`.

- **`502` on every request.** The Admin Server cannot reach the upstream. Check network egress to `upstream.base_url`, DNS resolution, and TLS interception by corporate proxies.

- **`403 {"error":"path not permitted"}`.** A `forwarding` rule is blocking that path. Adjust `forwarding.mode` / `forwarding.rules` if the path should egress.
