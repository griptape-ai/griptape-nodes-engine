# Admin Server

The Admin Server runs inside your network and acts as the single host that talks to Griptape Cloud on behalf of your Griptape Nodes Application instances. Your instances point at the Admin Server instead of `cloud.griptape.ai` directly, and it forwards each request upstream. This lets you keep individual application instances off the public internet while they continue to license, run sessions, and use Griptape Cloud features.

## Why use it

A studio that deploys Griptape Nodes on-premises usually does not want every application instance reaching the public internet on its own. The Admin Server gives you one place to manage that connection:

- **Lock instances down.** Application instances talk only to the Admin Server, so they never need direct internet egress.
- **One egress point.** Instead of opening internet access for each instance, you allow a single host through your firewall — one rule to manage and one place to audit outbound traffic.
- **Central configuration.** You configure where Griptape Cloud lives and, optionally, which Cloud paths are allowed to leave your network — in one spot rather than per instance.

If your application instances can already reach `cloud.griptape.ai` directly and that is acceptable for your environment, you do not need the Admin Server.

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

A complete `config.yaml` with the default values looks like this:

```yaml
server:
  host: "0.0.0.0"
  port: 8080
  read_timeout: "30s"
  write_timeout: "30s"
  shutdown_timeout: "10s"

upstream:
  base_url: "https://cloud.griptape.ai"
  timeout: "30s"
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

| Key                | Default   | Description                                                           |
| ------------------ | --------- | --------------------------------------------------------------------- |
| `host`             | `0.0.0.0` | Address the server listens on.                                        |
| `port`             | `8080`    | Port the server listens on.                                           |
| `read_timeout`     | `30s`     | HTTP read timeout (a duration string, e.g. `30s`).                    |
| `write_timeout`    | `30s`     | HTTP write timeout.                                                   |
| `shutdown_timeout` | `10s`     | Deadline for in-flight requests to finish during a graceful shutdown. |

### upstream

| Key           | Default                     | Description                                                                     |
| ------------- | --------------------------- | ------------------------------------------------------------------------------- |
| `base_url`    | `https://cloud.griptape.ai` | The Griptape Cloud root the server forwards to.                                 |
| `timeout`     | `30s`                       | How long to wait for the upstream to start responding.                          |
| `api_key_env` | `GT_CLOUD_API_KEY`          | The **name** of the environment variable that holds the Griptape Cloud API key. |

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

| Variable                  | Default                     | Description                                                                   |
| ------------------------- | --------------------------- | ----------------------------------------------------------------------------- |
| `GT_CLOUD_API_KEY`        | _(required)_                | The Griptape Cloud API key (or the variable named by `upstream.api_key_env`). |
| `SERVER_HOST`             | `0.0.0.0`                   | Listen address.                                                               |
| `SERVER_PORT`             | `8080`                      | Listen port.                                                                  |
| `SERVER_READ_TIMEOUT`     | `30s`                       | HTTP read timeout.                                                            |
| `SERVER_WRITE_TIMEOUT`    | `30s`                       | HTTP write timeout.                                                           |
| `SERVER_SHUTDOWN_TIMEOUT` | `10s`                       | Graceful shutdown deadline.                                                   |
| `UPSTREAM_BASE_URL`       | `https://cloud.griptape.ai` | Upstream Griptape Cloud root.                                                 |
| `UPSTREAM_TIMEOUT`        | `30s`                       | Upstream response-header timeout.                                             |
| `UPSTREAM_API_KEY_ENV`    | `GT_CLOUD_API_KEY`          | Name of the env var holding the API key.                                      |
| `LOG_LEVEL`               | `info`                      | Log verbosity.                                                                |
| `LOG_FORMAT`              | `json`                      | Log output format.                                                            |
| `FORWARDING_MODE`         | `allow_all`                 | `allow_all`, `allow`, or `deny`.                                              |
| `FORWARDING_RULES`        | _(empty)_                   | Comma-separated list of paths to allow or deny.                               |

## Running it

1. Set the Griptape Cloud API key in the environment:

    ```bash
    export GT_CLOUD_API_KEY="gt-..."
    ```

1. Provide a `config.yaml` (see [Configuration](#configuration)) and start the Admin Server. By default it listens on `0.0.0.0:8080` and forwards to `https://cloud.griptape.ai`.

1. Point your Griptape Nodes Application instances at the Admin Server's address instead of `cloud.griptape.ai`.

If the API key is missing or invalid, or the upstream cannot be reached, the Admin Server logs the reason and exits without serving — so configuration problems surface at startup.

## Troubleshooting

- **The server won't start.** Confirm `GT_CLOUD_API_KEY` is set and valid; the Admin Server validates it at startup and fails closed if it cannot.
- **`502` on every request.** The Admin Server cannot reach the upstream. Check network egress to `upstream.base_url`, DNS resolution, and TLS interception by corporate proxies.
- **`403 {"error":"path not permitted"}`.** A `forwarding` rule is blocking that path. Adjust `forwarding.mode` / `forwarding.rules` if the path should egress.
