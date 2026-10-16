# Transports: Connecting into the Engine

The Griptape Nodes engine is the program that runs your workflows. The editor is one way to talk to it, but not the only one: pipeline scripts, a panel inside your DCC, a studio dashboard, or a command in a terminal can all send the engine requests and read back the results.

A **transport** is the connection an external program uses to reach the engine. If you have set up an [MCP server](mcp/index.md), you have already picked a transport there (stdio, SSE, streamable HTTP, WebSocket); engine transports are the same idea, pointed the other way. Instead of the engine connecting out to a server, your program connects in to the engine.

## Choosing a transport

| Transport          | Reach it from                    | Talks back? | On by default                             | Good for                                                        |
| ------------------ | -------------------------------- | ----------- | ----------------------------------------- | --------------------------------------------------------------- |
| `local_socket`     | Programs on the same machine     | Yes         | Yes                                       | The `gtn request` command, local agents and scripts             |
| `websocket_direct` | `ws://127.0.0.1:18125`           | Yes         | No                                        | The editor on the same machine, DCC panels, custom frontends    |
| `http_direct`      | `http://127.0.0.1:18126`         | No          | No                                        | Fire-and-forget scripts, webhooks, farm hooks, simple web pages |
| `websocket_api`    | Anywhere, through Griptape Cloud | Yes         | Yes, when a Griptape Cloud API key is set | An editor in a browser driving an engine on a different machine |

A few rules of thumb:

- **Staying on one machine?** Use `local_socket` or `websocket_direct`. Nothing leaves the machine.
- **Just need to kick something off?** Use `http_direct`. Any program that can send an HTTP POST can use it, with nothing to keep open.
- **Need the answer back?** Use `local_socket` or `websocket_direct`. `http_direct` only receives: it confirms your request arrived, and the result is delivered over a transport that can send (see [Getting results back](#getting-results-back)).
- **Editor and engine on different machines?** Use `websocket_api`. See [How the editor reaches the application](../architecture.md#how-the-editor-reaches-the-application).

You can run several transports at once. The engine answers on all of them.

The local transports (`local_socket`, `websocket_direct`, `http_direct`) only start when the engine has a license or a Griptape Cloud API key (`GT_CLOUD_API_KEY`, which `gtn init` sets up). Without either, the engine logs that the transport needs one and leaves it off.

## Turning transports on and off

Transports are set under the `transports` key of a `griptape_nodes_config.json` file, keyed by transport name. Name only the transports you want to change; the rest keep their defaults. For example, to turn on both direct transports:

```json
{
    "transports": {
        "websocket_direct": {"enabled": true},
        "http_direct": {"enabled": true}
    }
}
```

Restart the engine after changing a transport. See [Engine Configuration](configuration.md) for where config files live and the order they load in.

You can also set a transport from the environment, with the [`GTN_CONFIG_` overrides](configuration.md#environment-variable-overrides). The path is `transports`, then the transport name, then the setting, joined with double underscores:

```bash
GTN_CONFIG_TRANSPORTS__HTTP_DIRECT__ENABLED=true gtn
GTN_CONFIG_TRANSPORTS__WEBSOCKET_DIRECT__PORT=19125 gtn
```

`allowed_origins` is a list, so like other list settings it can only be set in a config file. Setting it from the environment turns `http_direct` off with a warning.

### Settings

| Setting                                     | Default                              | What it does                                                                                                                                                |
| ------------------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `transports.local_socket.enabled`           | `true`                               | Serve the local socket (a named pipe on Windows).                                                                                                           |
| `transports.websocket_direct.enabled`       | `false`                              | Serve the local WebSocket.                                                                                                                                  |
| `transports.websocket_direct.host`          | `127.0.0.1`                          | Address to listen on.                                                                                                                                       |
| `transports.websocket_direct.port`          | `18125`                              | Port to listen on.                                                                                                                                          |
| `transports.websocket_direct.advertise_url` | none                                 | The `ws://` or `wss://` address other programs should use to reach this engine, when that differs from the listen address (see below).                      |
| `transports.http_direct.enabled`            | `false`                              | Serve the local HTTP endpoint.                                                                                                                              |
| `transports.http_direct.host`               | `127.0.0.1`                          | Address to listen on.                                                                                                                                       |
| `transports.http_direct.port`               | `18126`                              | Port to listen on.                                                                                                                                          |
| `transports.http_direct.allowed_origins`    | `[]`                                 | Web pages allowed to call `http_direct` from a browser, in addition to the Griptape Nodes editors. See [Calling from a web page](#calling-from-a-web-page). |
| `transports.websocket_api.enabled`          | `true` with an API key, else `false` | Connect to Griptape Cloud so a remote editor can reach this engine. Needs `GT_CLOUD_API_KEY`.                                                               |

`advertise_url` matters when the engine listens on one address but is reached at another, such as when it runs in a container, behind port mapping, or listens on `0.0.0.0`. For example, `"advertise_url": "ws://render-node-12.local:18125"`.

!!! warning "Keep the default host unless you need it"

    The default host, `127.0.0.1`, only accepts connections from the same machine. Setting `host` to `0.0.0.0` or a network address lets any machine that can reach that port send requests to your engine. Only do this on a network you trust.

    Even on `127.0.0.1`, any program running on your machine can connect to the local transports. Turn on only the ones you use.

### When a setting is wrong

The engine checks each transport's settings at startup. A transport with a bad value, such as a port out of range or an `allowed_origins` entry that isn't an origin, is **turned off** and the engine logs a warning naming the transport and the problem. The other transports still start. A misspelled setting name, or a transport name the engine doesn't know, is ignored with a warning.

`websocket_api` stays off if you enable it without a Griptape Cloud API key, again with a warning.

The engine needs at least one transport that can send results: `local_socket`, `websocket_direct`, or `websocket_api`. `http_direct` on its own is not enough.

## Sending requests

Every transport carries the same requests: the ones the editor sends to create nodes, set parameter values, run flows, and so on. A request is a JSON message like this:

```json
{
    "type": "EventRequest",
    "payload": {
        "event_type": "EventRequest",
        "request_type": "ListFlowsInCurrentContextRequest",
        "request_id": "my-request-1",
        "response_topic": "my-app/response",
        "request": {}
    }
}
```

- `request_type` names the request, and `request` holds its fields. For example, `StartFlowRequest` with `{}` runs the flow that's currently open, and `ListFlowsInCurrentContextRequest` lists the flows you can name instead.
- `request_id` is any string you choose. The result carries it back so you can match the two.
- `response_topic` is where the result is sent. Choose a name for your program and listen on it (see the next section).

[Retained Mode Scripting](../development/retained_mode.md) describes the same operations from Python inside the editor.

### Getting results back

Results are delivered on the `response_topic` you named, to every connection that has subscribed to it. On `websocket_direct` and `local_socket`, subscribe by sending:

```json
{"type": "subscribe", "topic": "my-app/response", "payload": {}}
```

Results then arrive as messages whose `type` is `success_result` or `failure_result`, with your `request_id` inside `payload`.

If your organization uses license keys, the engine checks each request against your license. A request your license doesn't permit is refused rather than run.

## Examples

### Send a request from the terminal

`local_socket` is easiest to use through the `gtn request` command. For your own scripts and apps, `websocket_direct` is simpler to connect to.

The `gtn request` command sends one request over `local_socket` to the engine running on your machine and prints the result. It finds the engine and handles subscribing for you, so there is nothing to configure:

```bash
gtn request '{"request_type": "ListFlowsInCurrentContextRequest"}'
```

Put the request's fields beside `request_type`. This is the simplest way to try a request before building it into a script, and it works well from shell scripts and local agents.

### Kick off a workflow from a pipeline script

A render-farm post-job script, a publish hook, or a webhook usually only needs to say "go". With `http_direct` turned on, that is one `curl` call:

```bash
curl -X POST http://127.0.0.1:18126/events \
  -H "Content-Type: application/json" \
  -d '{
        "type": "EventRequest",
        "payload": {
          "event_type": "EventRequest",
          "request_type": "StartFlowRequest",
          "request_id": "farm-job-1042",
          "response_topic": "farm/response",
          "request": {}
        }
      }'
```

The engine answers `202` as soon as it has the request:

```json
{"denials": []}
```

This runs the flow that's currently open in the engine. To run a different loaded flow, add its name to `request`, as in `{"flow_name": "..."}`, using a name from `ListFlowsInCurrentContextRequest`.

`202` means *accepted*, not *finished*. If the flow can't start, the failure goes to `farm/response`, so if the script needs the outcome, have another program subscribed to `farm/response` over `websocket_direct` or `local_socket`.

### Connect a DCC panel or custom frontend

A panel inside Nuke, Maya, Houdini, or your own app can hold a `websocket_direct` connection open, send requests, and receive results and progress as they happen. A minimal Python client, using the `websockets` package:

```python
import asyncio
import json

import websockets


async def main():
    async with websockets.connect("ws://127.0.0.1:18125") as ws:
        await ws.send(json.dumps({"type": "subscribe", "topic": "my-panel/response", "payload": {}}))
        await ws.send(
            json.dumps(
                {
                    "type": "EventRequest",
                    "payload": {
                        "event_type": "EventRequest",
                        "request_type": "ListFlowsInCurrentContextRequest",
                        "request_id": "panel-1",
                        "response_topic": "my-panel/response",
                        "request": {},
                    },
                }
            )
        )
        async for raw in ws:
            message = json.loads(raw)
            if message.get("payload", {}).get("request_id") == "panel-1":
                print(message)
                break


asyncio.run(main())
```

Turn on `websocket_direct` first. Griptape Nodes Desktop uses this transport for its own editor; its **Direct Engine WebSocket URL** setting must match `host` and `port` here (see [App Settings](desktop/app_settings.md)).

### Calling from a web page

A browser page, such as a small studio dashboard with a "Run" button, can POST to `http_direct`. Browsers only allow this from origins the engine trusts. The Griptape Nodes editors are always trusted; add your own page's origin to `allowed_origins`:

```json
{
    "transports": {
        "http_direct": {
            "enabled": true,
            "allowed_origins": ["http://localhost:3000", "https://dashboard.example.com"]
        }
    }
}
```

An origin is the scheme, host, and port only (`https://dashboard.example.com`, not `https://dashboard.example.com/admin/`). `*` is not allowed, since it would let any web page you visit send requests to your engine. An entry that isn't a plain origin, including `*` or one with a path, turns `http_direct` off with a warning.

From the page:

```javascript
await fetch("http://127.0.0.1:18126/events", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    type: "EventRequest",
    payload: {
      event_type: "EventRequest",
      request_type: "StartFlowRequest",
      request_id: "dashboard-1",
      response_topic: "dashboard/response",
      request: {},
    },
  }),
});
```

The engine refuses a page from any other origin with `403`. The browser reports this to the page as a network error; the browser's developer tools show the `403` and the setting that would allow the page. Scripts and programs outside a browser, such as `curl`, aren't affected by `allowed_origins`.

### Reach an engine on another machine

To drive an engine on a workstation or server from an editor somewhere else, use `websocket_api`. Both connect out to Griptape Cloud, which relays between them, so no ports need to be opened. It turns on automatically when the engine has a Griptape Cloud API key. See [Architecture](../architecture.md#editor-and-engine-on-different-machines).

## `http_direct` reference

`http_direct` accepts `POST /` or `POST /events` with `Content-Type: application/json`. The body must be the full request message shown in [Sending requests](#sending-requests), not the shorter form `gtn request` takes. A body in another shape still gets `202`, but nothing runs and the engine logs an error.

| Response | Meaning                                                                                                                                 |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `202`    | Accepted. The body lists any requests your license refused, as `{"denials": [...]}`.                                                    |
| `400`    | The body isn't valid JSON, isn't a JSON object, or is a `subscribe` message, which only the two-way transports accept.                  |
| `403`    | Refused: the page's origin isn't in `allowed_origins`, your license refused the connection, or it refused every request in the message. |
| `404`    | The path isn't `/` or `/events`.                                                                                                        |
| `405`    | The method isn't `POST`.                                                                                                                |
| `408`    | The body took more than 30 seconds to arrive.                                                                                           |
| `413`    | The body is larger than 64 MiB.                                                                                                         |
| `415`    | `Content-Type` isn't `application/json`.                                                                                                |
| `503`    | The engine couldn't take the request right now. Try again.                                                                              |

## Related pages

- [Engine Configuration](configuration.md) covers config files and environment overrides.
- [Architecture](../architecture.md) shows how the editor, engine, and Griptape Cloud connect.
- [MCP](mcp/index.md) covers the engine connecting out to MCP servers, the other direction.
