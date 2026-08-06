# Griptape Nodes: what the automation is actually talking to

Verified on macOS against desktop app 0.21.2, editor bundle 0.119.0, engine
from `griptape-nodes-engine`. Re-check version-specific details if they stop
matching.

## Process and origin map

```
Griptape Nodes.app (Electron 43)
├─ main process        src/main/index.ts
├─ renderer (shell)    file://.../app.asar/.webpack/renderer/main_window/index.html
│   └─ <webview>       gtn-editor://editor/index-editor.html?v=<editor version>
│        served by     src/main/editor-bundle.ts protocol handler
│        from disk     /Applications/Griptape Nodes.app/Contents/Resources/editor-bundle
└─ engine (bundled py) Resources/engine-bundle/python/bin/python3 -m griptape_nodes_app
     127.0.0.1:8124    static files + API  (bare / returns 404 by design)
     127.0.0.1:8125    secondary port
```

In CDP the shell is a `page` target and the editor is an `iframe` target.
`browser.getPage(<iframe id>)` fails with `Target.createTarget: Not supported`;
reach it via `shell.frames()` instead. Frame `evaluate`, `locator`, `click` and
`fill` all work through that handle. `Frame` has no `screenshot()` — screenshot
the shell page, which composites the editor into the image.

**Identify the frame by evaluating `location.href` inside it, not by
`frame.url()`.** Playwright reports `frame.url() === ""` for this webview OOPIF
most of the time (it populated once immediately after attach, then went empty and
stayed empty), while `await frame.evaluate(() => location.href)` reliably returns
`gtn-editor://editor/index-editor.html?v=…`. Filtering on `frame.url()` is a
silent no-match.

## CDP availability

`forge.config.ts` sets `EnableNodeCliInspectArguments: false`, so `--inspect`
and friends are dead. `--remote-debugging-port` is a Chromium switch, not
covered by that fuse, and works on the signed production build:

```bash
open -a "Griptape Nodes" --args --remote-debugging-port=9222
```

The flag only applies at launch. `open -a` on an already-running app just
focuses it, so the app has to be fully quit first — and a graceful
`osascript -e 'quit app ...'` can take longer than a few seconds, so wait for
the process to actually disappear before relaunching.

## Engine CORS allowlist

`griptape-nodes-engine/src/griptape_nodes/servers/static.py:311` allows:

- `https://app.nodes.griptape.ai` (override with `GRIPTAPE_NODES_UI_BASE_URL`)
- `https://app.nodes-staging.griptape.ai`, `https://app-nightly.nodes.griptape.ai`
- `https://editor.nodes.griptape.ai`, `https://editor-nightly.nodes.griptape.ai`
- `http://localhost:5173`, `http://localhost:5174`
- `gtn-editor://editor`

That is why the hosted web editor can drive the *local* engine: same engine,
different host page.

## Why self-hosting the editor bundle does not work

Serving `editor-bundle` over `http://localhost:5173` returns 200 for both
`index.html` and `index-editor.html`, and the app boots — then renders
**"Failed to connect / Parent frame did not respond within timeout"**.

The bundle is an iframe guest expecting a `@griptape-ai/nodes-bridge` parent.
The desktop side of that contract is
`src/renderer/providers/ParentBridgeProvider.tsx` wrapped around
`EditorWebview.tsx`; the hosted web app implements its own. Standing up a
third host means reimplementing the bridge, auth token plumbing and session
management. Not worth it — both existing hosts are already reachable.

## Auth

- **Desktop**: already signed in. Tokens live in the `persist:main` partition and
  are brokered to the webview by the main process. Nothing to do.
- **Web**: Auth0 at `auth.cloud.griptape.ai`. `dev-browser` named browsers keep a
  persistent profile under `~/.dev-browser/browsers/<name>`, and daemon-launched
  Chromium is **headed by default** (`--headless` is opt-in), so a human can log
  in once and the cookies survive. Never automate the credential entry.

## Screen recording permissions (two tiers)

`gtn-record-screen.sh` needs macOS "Screen Recording" granted to the terminal
app (System Settings → Privacy & Security → Screen Recording), which requires
fully restarting the terminal after granting it — a `screencapture -x` probe
confirms this tier.

Separately, the *first* `avfoundation` capture can trigger a per-app TCC
prompt — *"iTerm is requesting to bypass the system private window picker and
directly access your screen and audio"*. This is Apple's newer
per-capture-session consent, distinct from the Screen Recording toggle. It is
one-time: once allowed, subsequent captures proceed silently. Never click this
dialog on the user's behalf.

The avfoundation screen device only offers `uyvy422 / yuyv422 / nv12 / 0rgb /
bgr0`, not `yuv420p`. Passing `-pixel_format nv12` on the input avoids a
"Selected pixel format ... is not supported" warning; ffmpeg auto-negotiates
even without it, so the warning was cosmetic, not a failure. Separately,
`objc[...]: class 'NSKVONotifying_AVCaptureScreenInput' not linked into
application` on stderr is unrelated framework noise and can be ignored.

## One CDP session at a time — but concurrency within one script is fine

Two **separate `dev-browser` processes** attached to the desktop app
simultaneously do not both see live pixels. Measured: a passive recorder
looping `screenshot()` in one process while a second process typed into the
workflow search box produced 90 frames that *all* showed the pre-interaction
state — never the typing, never the cleared field — while the interacting
script's own screenshot showed the change correctly.

The same passive loop run **alone** is fresh: 12 frames over 11s tracked the
self-updating CPU/MEM readout in the shell header, with differing md5s. So the
defect is cross-process concurrency specifically, not screenshot staleness in
general — record interactions from inside the driving script (the storyboard
or smooth-flow pattern in SKILL.md), never from a second process.

This does **not** rule out concurrency *within* a single script/process. The
smooth-flow recording pattern runs a screenshot-capture loop and the actual
click/type actions as two `async` functions raced with `Promise.all` inside
one `dev-browser` invocation — one CDP connection, two logical tasks
interleaved on it. Verified safe: typed into a field while a capture loop ran
concurrently on a 60ms timer, and the field's final value came back correct
afterwards (no corruption from interleaved commands). The cost is throughput,
not correctness — see "Capture throughput" in SKILL.md's smooth-flow section;
input and screenshot commands share the connection, so capture measurably
slows down while typing is also happening.

## Controlling the graph over MCP instead of clicks

The engine starts a Streamable HTTP MCP server unconditionally on
`AppInitializationComplete` — see
`griptape-nodes-engine/src/griptape_nodes/retained_mode/managers/agent_manager.py:367-372`,
which threads `start_mcp_server` from
`griptape-nodes-engine/src/griptape_nodes/servers/mcp.py`. Default address is
`http://localhost:8125/mcp/` (`GTN_MCP_SERVER_HOST`/`GTN_MCP_SERVER_PORT` env
vars override; the comment in `mcp.py` notes the port is deliberately stable so
external MCP clients can hard-code it). No auth on the endpoint — acceptable
because it's loopback-only.

It is registered in this machine's Claude Code config as the `griptape-nodes`
MCP server, added with:

```bash
claude mcp add --transport http griptape-nodes http://localhost:8125/mcp/ --scope user
```

`claude mcp list` should show `griptape-nodes: ... ✔ Connected`. **The tool
list only loads into a session at session start** — a session already running
when the server was added won't see `mcp__griptape-nodes__*` tools via
`ToolSearch` until it restarts. This is a Claude Code client-side limitation,
not a server problem; the HTTP endpoint itself is reachable immediately.

Every `SUPPORTED_REQUEST_EVENTS` entry in `mcp.py` becomes an MCP tool named
after the request class (e.g. `CreateNodeRequest`, `CreateConnectionRequest`,
`SetParameterValueRequest`, `ListNodesInFlowRequest`,
`ListConnectionsForNodeRequest`, `GetAllNodeInfoRequest`), plus a synthetic
`EventRequestBatch` tool that takes `{requests: [{request_type, request}, ...]}`
and dispatches them as one round trip — worth using for any multi-step build
(several `CreateNodeRequest` + `CreateConnectionRequest` calls) instead of one
tool call per step.

Verified by round-trip: built a `Load Video → CorridorKey Video Inference`
graph via UI clicks (see below), then called `ListNodesInFlowRequest` and
`ListConnectionsForNodeRequest` over raw JSON-RPC against the MCP endpoint and
got back exactly that graph — proving MCP and the UI are two views onto the
same engine state, and that either can be used to build or verify it.

Manual JSON-RPC handshake, useful for debugging the server directly without
going through an MCP client (Streamable HTTP requires a session id from
`initialize` echoed back on every subsequent call):

```bash
SID=$(curl -s -X POST http://127.0.0.1:8125/mcp/ \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}}}' \
  -D - -o /dev/null | grep -i mcp-session-id | tr -d '\r' | awk '{print $2}')

curl -s -X POST http://127.0.0.1:8125/mcp/ \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -s -X POST http://127.0.0.1:8125/mcp/ \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ListNodesInFlowRequest","arguments":{}}}'
```

Responses come back as SSE (`event: message\ndata: {...}`), one JSON-RPC
envelope per line.

## Claude Code cannot render video inline

Researched directly (Claude Code artifacts docs + VS Code extension guide),
not assumed: Artifacts only support `.html`/`.htm`/`.md` sources and run under
a CSP that blocks external requests, so a `<video>` tag would need its source
inlined as a data URI — impractical for real recordings. Neither the VS Code
chat panel nor the `Read` tool has any other video-rendering path. This holds
for mp4 and webm alike; there is no format-level workaround, only the
GIF-conversion fallback in SKILL.md's "Showing results in chat" section.

The GIF path uses ffmpeg's two-pass palette technique
(`palettegen`/`paletteuse`) rather than letting the encoder pick a palette
per-frame — flat-color UI screenshots with sharp text band and dither
noticeably without it. Verified: a 12-frame, 1.77s smooth-flow capture
round-tripped through `gtn-to-gif.sh` produced an 82KB GIF that rendered
inline via `Read` (confirmed as an image render; verifying the animation
itself plays back in a live VS Code panel is outside what this environment
can check directly — ask the user to confirm if it matters).

## Building a graph via UI

Confirmed working end to end: created a workflow, added two nodes via the
quick-add palette, and drew a data edge between them, purely over CDP.

**New workflow**: the "Workflows" launcher dialog and a second "Create New
Workflow" dialog can be stacked, so `button:has-text("Create New Workflow")`
matches two elements — one of them behind the topmost dialog, which Playwright
will happily click-timeout on ("subtree intercepts pointer events") rather than
fail fast. Use `.nth(1)` (the front one) or scope the locator to the visible
dialog's container.

**Adding a node — prefer the quick-add palette over dragging from the sidebar.**
Click an empty point on the canvas to focus it, then press `Tab`: a
`Search nodes, or #tag + space...` palette opens *at that point*, pre-populated
with recent/category results. Type the node name, the top match highlights
automatically, press `Enter` to place it there. This avoids simulating an
HTML5/pointer drag from the library sidebar entirely. The node lands exactly
where you clicked, which is often off the visible viewport for a second node —
follow up with the `Fit View` control (`button[title="Fit View"]` via the frame
locator) to bring everything back into frame.

**Connecting two nodes (drawing an edge):** find the port `<div>`s directly —
```js
const handles = editor.locator('.react-flow__handle');
```
Each React Flow node renders **two** handle elements per parameter (one
`target` on the left, one `source` on the right), both sharing the same
`data-handleid`, so filtering by id alone is ambiguous. Disambiguate by DOM
order (`.nth(i)`, found via an `evaluate` dump of
`data-handleid`/`className`/closest `.react-flow__node` text first) or by
matching `.source`/`.target` in `className` together with the id.

Do **not** use `getBoundingClientRect()` coordinates from an `evaluate()` call
for the drag — those are relative to the iframe's own document, not the shell
page, and feeding them straight into `shell.mouse.move()` silently drags in the
wrong place. Instead call `.boundingBox()` on the **Playwright frame locator**
itself (`editor.locator(...).nth(i).boundingBox()`); Playwright documents this
as already translated to the top-level page, and it matched observed pixels
correctly. Then drive the connection with the shell page's mouse, not the frame:

```js
await shell.mouse.move(sx, sy);
await shell.mouse.down();
await shell.mouse.move(midX, midY, { steps: 10 });
await shell.mouse.move(dx, dy, { steps: 10 });
await shell.mouse.up();
```

Confirm the edge landed on the intended ports by reading it back, rather than
trusting the drag succeeded:

```js
await editor.evaluate(() => document.querySelector('.react-flow__edge')?.getAttribute('data-testid'));
// "rf__edge-<source node>-<source handle>-<target node>-<target handle>-<id>"
```

That `data-testid` names both endpoints explicitly and is the cheapest
correctness check available — cheaper than screenshotting and eyeballing the
wire.

## dev-browser notes

- Scripts run in a QuickJS sandbox: no `require`, `import`, `fs`, `fetch`,
  `process`. Helpers cannot be imported from a library file — paste snippets.
- File I/O is confined to `~/.dev-browser/tmp/` via `saveScreenshot`,
  `writeFile`, `readFile`, all async.
- Default script timeout is 30s; raise it with `--timeout` for capture loops.
- Playwright's `recordVideo` is not reachable (no context options are exposed),
  hence the screenshot-loop-plus-ffmpeg approach in `gtn-record.sh`.

## Useful local paths

| Thing | Path |
|---|---|
| Desktop app source | `~/dev/griptape-nodes-desktop` |
| Engine source | `~/dev/griptape-nodes-engine` |
| App/CLI source | `~/dev/griptape-nodes-app` |
| Engine MCP server | `~/dev/griptape-nodes-engine/src/griptape_nodes/servers/mcp.py` |
| Workspace / workflows | `~/GriptapeNodes` |
| App logs | `~/dev/griptape-nodes-desktop/_logs`, `~/Library/Application Support/Griptape Nodes` |
| Editor bundle (packaged) | `/Applications/Griptape Nodes.app/Contents/Resources/editor-bundle` |
| Editor bundle (dev) | `~/dev/griptape-nodes-desktop/resources/editor-bundle` |
