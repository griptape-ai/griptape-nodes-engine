---
name: gtn-verify
description: Drive or verify Griptape Nodes. Use for anything touching a Griptape Nodes graph — building/editing workflows, inspecting node/connection/parameter state, or visually verifying features with screenshots and video. Use after changing anything that renders in the Griptape Nodes editor or desktop shell, or when asked to "show me", "screenshot", "record", "check it in the app", "add a node", "connect these nodes", "verify in the desktop app", or "verify in the web editor". Graph construction and inspection go through the `griptape-nodes` MCP server; only actual visual/UI checks go over CDP to the desktop app or a Chromium session on the hosted web editor.
---

# Working with Griptape Nodes

## Prerequisites

**macOS, Linux, and Windows are all supported**, but through different
scripts. Every script in `scripts/` has two forms:

- `gtn-foo.sh` — macOS and Linux (bash). Run directly: `./gtn-foo.sh --flag value`.
- `gtn-foo.ps1` — Windows (native PowerShell, no WSL/Git Bash needed). Run via
  `pwsh`, with PowerShell-style named parameters instead of `--flags`:
  `pwsh ./gtn-foo.ps1 -Flag value`.

Examples throughout this doc show the `.sh` form; on Windows, swap the
extension and flag style. The one exception is the raw `dev-browser <<EOF ...
EOF` heredoc pattern used to drive the app directly (not a bundled script) —
on Windows, pipe a here-string to stdin instead:

```powershell
$script = @"
... same JS body ...
"@
$script | dev-browser --connect http://localhost:9222 --timeout 120
```

macOS is the most-exercised platform (native screen recording, app
launch/quit, and permission handling were all built and verified there
first). The Linux and Windows equivalents for desktop app launch/quit and
native screen recording are best-effort — see the comments at the top of
`gtn-desktop-up.sh`/`.ps1` and `gtn-record-screen.sh`/`.ps1` for the exact
caveats (e.g. Linux desktop app install-path detection, and `wf-recorder`
only supporting wlroots Wayland compositors, not GNOME/KDE). The Web pathway,
MCP tool calls, and the CDP-screenshot-loop recording scripts
(`gtn-record.*`, `gtn-encode.*`, `gtn-to-gif.*`, `gtn-trim-pauses.*`,
`gtn-clean-scratch.*`) do not depend on any of that and are equally solid
everywhere.

One-time setup per machine — run the installer for your OS, then fill in
anything it can't automate:

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-install-prereqs.sh   # macOS/Linux
```

```powershell
pwsh ${CLAUDE_SKILL_DIR}/scripts/gtn-install-prereqs.ps1   # Windows
```

It installs `ffmpeg`, `python3`, `node`/`npm`, and the `dev-browser` CLI
(+ its bundled Chromium) via the OS package manager (`brew`/`apt`/`dnf`/
`pacman`/`zypper` on macOS/Linux, `winget`/`choco` on Windows), and on Linux
under a Wayland session, `wf-recorder` if available. It does **not** install
Homebrew/Chocolatey if neither package manager is present, and does not
install the Griptape Nodes Desktop app itself (a GUI download, not a
package) — it only checks for these and tells you what's missing.

| Tool | Check | Notes |
|---|---|---|
| `dev-browser` CLI | `dev-browser --help` | Only strictly needed for the Web pathway — the Desktop pathway attaches to the already-running app's own browser, not dev-browser's |
| `ffmpeg` | `ffmpeg -version` | Needs the `mpdecimate` filter and `libx264`, both in a standard build |
| `python3` | `python3 --version` | Ships with macOS; installed by the script elsewhere |
| Griptape Nodes Desktop app | see per-OS install-path notes in `gtn-desktop-up.sh`/`.ps1` | Install normally, for the Desktop pathway. On Linux/Windows, set `GTN_DESKTOP_BIN` (`$env:GTN_DESKTOP_BIN` on Windows) if it's installed somewhere the scripts don't guess |
| `griptape-nodes` MCP server | `claude mcp list` shows it Connected | With the engine running: `claude mcp add --transport http griptape-nodes http://localhost:8125/mcp/ --scope user` — see [Prefer engine state over UI clicks](#prefer-engine-state-over-ui-clicks) |
| Screen Recording permission (macOS only) | try `gtn-record-screen.sh` once | System Settings → Privacy & Security → Screen Recording → grant to your terminal app, then fully restart it |
| `wf-recorder` (Linux + Wayland only) | try `gtn-record-screen.sh` once | Only works on wlroots compositors (Sway, Hyprland, river); on GNOME/KDE Wayland, use an X11/XWayland session instead |

Versions this was built and verified against: `dev-browser` 0.2.9, `ffmpeg`
8.1.2, `claude` 2.1.221, Node 22, Python 3.14. Nothing here relies on
version-specific behavior, but if something breaks after an update, check
here first.

There are two fundamentally different ways to touch Griptape Nodes, and picking
the right one matters:

- **MCP (`mcp__griptape-nodes__*` tools)** — direct calls into the running
  engine's retained-mode API. Use this for anything that is really about *graph
  state*: adding/removing nodes, wiring connections, setting parameter values,
  reading back what exists. No clicking, no coordinates, no flaky selectors.
- **Browser automation (CDP + `dev-browser`)** — drives the actual rendered UI.
  Use this only when the thing under test *is* the UI: does the click land,
  does the widget render, does the drag work, does the screenshot look right.

**Default to MCP whenever the user is not specifically asking you to click
something.** "Add a LoadVideo node and connect it to X" is a graph-construction
task — do it over MCP, then use the browser pathway only if you also need a
screenshot of the result. Reserve UI automation for requests like "test that
the new widget responds to clicks" or "verify this renders correctly", where
the interaction or the pixels are literally what's being checked. See
[Prefer engine state over UI clicks](#prefer-engine-state-over-ui-clicks) below
for the MCP tool reference.

## Visual verification: two pathways

Two independent pathways reach the same editor for **visual** checks. Pick one
with the routing rule below, then follow that pathway's section.

## Routing rule

| Situation | Pathway |
|---|---|
| User says "desktop", "the app", "Griptape Nodes Desktop" | **Desktop** |
| User says "web", "browser", "hosted editor", "app.nodes.griptape.ai" | **Web** |
| Testing desktop shell chrome: menus, engine popover, update banner, settings, native dialogs | **Desktop** |
| Testing hosted-editor-only behaviour or browser compatibility | **Web** |
| **Unspecified** | **Desktop** |

Desktop is the default because it is strictly cheaper for the same result: the
app is normally already running and already signed in, one screenshot of the
shell captures the editor too, and it is the artifact that actually ships. The
web pathway needs a login that expires and gives no extra signal for editor-level
work. Only reach for Web when the question is specifically about the web product.

Do not run both pathways to cross-check unless the user asks for it.

## Desktop pathway

### 1. Bring the app up with CDP

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-desktop-up.sh
```

Idempotent. If CDP is already live on 9222 it returns immediately; otherwise it
quits and relaunches the app with `--remote-debugging-port=9222` and waits for
the engine and the editor frame. **Relaunching loses unsaved canvas state** — if
the user has the app open and did not authorise a restart, ask first.

### 2. Drive it

The desktop window is one CDP page (the Electron shell). The editor lives in a
child frame on the `gtn-editor://` origin. Get both like this — this preamble
goes at the top of every desktop script:

```bash
dev-browser --connect http://localhost:9222 <<'EOF'
const pages = await browser.listPages();
const shell = await browser.getPage(pages[0].id);
let editor = null;
for (const f of shell.frames()) {
  try {
    if ((await f.evaluate(() => location.href)).startsWith("gtn-editor://")) { editor = f; break; }
  } catch (e) { /* detached frame */ }
}
if (!editor) throw new Error("editor frame not attached — is the engine running?");

// ... work here ...
EOF
```

Match on `location.href` evaluated **inside** the frame, exactly as above. Do not
match on `frame.url()` — for this webview it usually returns an empty string, so
`frames().find(f => f.url().startsWith("gtn-editor://"))` silently finds nothing.

- `shell` is a full Playwright `Page`: `click`, `fill`, `locator`, `screenshot`,
  `mouse`, `keyboard`.
- `editor` is a Playwright `Frame`: `locator`, `evaluate`, `click`, `fill`. It has
  no `screenshot()` — screenshot the `shell`, which renders the editor inside it.
  For keyboard input, go through `shell.keyboard` after clicking the target.
- `browser.getPage(<iframe targetId>)` does **not** work (`Target.createTarget:
  Not supported`). Always go through `shell.frames()`.

### 3. Capture

Screenshot:

```js
console.log(await saveScreenshot(await shell.screenshot(), "gtn-scratch.png"));
```

Then `Read` the returned path to actually look at it. Saving a screenshot you
never read verifies nothing. Use a fixed scratch filename like this (overwrite
in place) unless the screenshot is an actual deliverable — see
[Storage: scratch vs permanent](#storage-scratch-vs-permanent).

Video: see [Recording](#recording).

## Web pathway

### 1. Bring the session up

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-web-up.sh
```

Uses the persistent named browser `gtn-web`, which is headed by default so a
human can interact with it. The script reports `state=ready` or `state=login`.

On `state=login`, stop and tell the user: *"The `gtn-web` browser profile needs a
one-time login — a Chromium window is open at the Griptape login page, please
sign in and tell me when done."* Cookies persist in that profile afterwards. Do
not attempt to automate the Auth0 login or handle credentials.

### 2. Drive it

```bash
dev-browser --browser gtn-web <<'EOF'
const page = await browser.getPage("hosted");
// ... work here ...
console.log(await saveScreenshot(await page.screenshot(), "gtn-scratch.png"));
EOF
```

Here `page` is the whole editor, so `page.screenshot()` and `page.locator()`
target it directly — no frame hop.

The hosted editor talks to the **same local engine** on `127.0.0.1:8124`, which
allowlists `app.nodes.griptape.ai` in CORS. So local engine changes are visible
through the web pathway too.

### What does not work

Serving `editor-bundle` yourself (e.g. on `localhost:5173`) and loading
`index.html` or `index-editor.html` directly fails with *"Parent frame did not
respond within timeout"*. The bundle is an iframe guest that requires a
`@griptape-ai/nodes-bridge` parent. Do not spend time on this route.

## Screenshot or video?

Default to a **screenshot**. It's cheaper to produce, cheaper to review, and
covers the large majority of "did this work" checks — most rendering, layout,
and one-shot state questions don't need motion to answer.

Reach for **video** only when one of these is true:

- The user explicitly asked for a recording/video/demo.
- The thing under test is inherently temporal and a still frame can't show
  it: dragging, a progress bar or spinner advancing, a live value updating, an
  animation, or a multi-step interaction where the sequence itself matters
  (not just the end state).

When in doubt, take the screenshot. If it turns out not to be enough (the
reviewer needs to see something move), upgrade to video then — that's a cheap
correction. Producing an unnecessary recording is not: it costs a pre-planning
pass, a script, an encode step, and a GIF conversion if it needs to be shown
inline (see "Showing results in chat" below), all for a question a screenshot
would have answered.

## Recording

**Never run two `dev-browser` scripts against the desktop app at once.** The
second session's screenshots come back stale — it silently captures the surface
as it was before the other session's changes, so the video looks like nothing
happened. This is the single biggest trap here. It rules out the obvious
"recorder in the background, interaction in the foreground" pattern.

### Recording a smooth interaction — plan once, execute once

A recording only looks "choppy" from real dead time getting captured — most
often, this agent's own thinking time between separate tool calls. The fix is
architectural, not a filter: **do all discovery first, unrecorded, then write
the entire interaction as one pre-planned script and run it in a single tool
call.** Nothing outside that one call is ever in the footage, so there is no
thinking-gap to remove in the first place. Verify once at the end by watching
the output — don't screenshot-and-decide mid-recording.

Within that single script, capture frames on a wall-clock timer running
*concurrently* with the actions (`Promise.all`), not one frame per action.
Sampling on a timer means playback speed can be made to match how long the
interaction actually took, instead of the plain storyboard pattern's arbitrary
fixed fps (which stretches or squashes real timing to whatever number was
picked). This is safe — screenshot and input commands share the same CDP
connection but do not corrupt each other; tested by typing while capturing on
a 60ms timer concurrently, with the final input value confirmed correct
afterwards.

```bash
dev-browser --connect http://localhost:9222 --timeout 120 <<'EOF'
// ... the shell + editor preamble from "Drive it" goes here ...

const frames = [];
let recording = true;
// Clip to the region under test when you know it — see "Capture throughput"
// below for why this matters.
const clip = { x: 0, y: 90, width: 420, height: 700 };

async function recorder(intervalMs) {
  while (recording) {
    const buf = await shell.screenshot({ type: "jpeg", quality: 70, clip });
    await saveScreenshot(buf, "gtnrec_" + String(frames.length).padStart(5, "0") + ".jpg");
    frames.push(Date.now());
    await shell.waitForTimeout(intervalMs);
  }
}

async function actions() {
  const t0 = Date.now();
  // ... the full pre-planned interaction, known from an earlier unrecorded pass ...
  await someField.click();
  await shell.keyboard.type("hello", { delay: 60 });
  await shell.waitForTimeout(500);
  recording = false;
  return Date.now() - t0;
}

const [, durationMs] = await Promise.all([recorder(60), actions()]);
console.log("frames=" + frames.length + " durationMs=" + durationMs);
EOF

${CLAUDE_SKILL_DIR}/scripts/gtn-encode.sh --duration-ms <durationMs from the log line> --out /tmp/demo.mp4
```

`--duration-ms` tells `gtn-encode.sh` to compute `fps = frame_count /
(duration_ms / 1000)` instead of using a fixed `--fps`, so the output video's
duration matches the real interaction time almost exactly (measured: 1584ms
requested, 1583.8ms produced).

**Capture throughput — set expectations, don't chase 30fps.** Screenshotting a
Griptape Nodes window is not free, and it gets slower, not faster, while input
is also happening on the same connection:

| Scenario | Measured throughput |
|---|---|
| Full-page screenshot, idle | ~180ms/frame (~5.5fps) |
| Clipped screenshot (small region), idle | ~62–75ms/frame (~13–16fps) |
| Clipped screenshot, concurrently with typing/clicking | ~300ms/frame (~3fps) |

Clip to the smallest region that covers the interaction (the sidebar, one
node, the area between two nodes) whenever you know it in advance — it's the
single biggest lever. ~3fps during active input is normal and fine: the goal
is eliminating dead air and getting correct pacing, not cinema-grade
smoothness. Don't spend time chasing a higher rate than the interaction needs.

### Recording an interaction — plain storyboard pattern

For a quick single-shot check where wall-clock pacing doesn't matter (e.g. one
before/after pair, or a handful of discrete states rather than continuous
motion), one frame per action and a fixed fps is simpler than the smooth-flow
pattern above:

```bash
dev-browser --connect http://localhost:9222 --timeout 120 <<'EOF'
// ... the shell + editor preamble from "Drive it" goes here ...
let n = 0;
async function snap() {
  await saveScreenshot(await shell.screenshot({ type: "jpeg", quality: 70 }),
                       "gtnrec_" + String(n++).padStart(5, "0") + ".jpg");
}

const field = editor.locator('input[placeholder*="Search workflows"]');
await field.click();               await snap();
await shell.keyboard.type("ck");   await snap();
await shell.waitForTimeout(600);   await snap();
console.log("frames:", n);
EOF

${CLAUDE_SKILL_DIR}/scripts/gtn-encode.sh --fps 4 --out /tmp/demo.mp4
```

Raise `--timeout` past the default 30s for anything long. `gtn-encode.sh`
clears the frames afterwards unless given `--keep`.

### Fallback: trimming pauses out of an already-recorded video

Sometimes a recording unavoidably has to span multiple agent turns — most
notably a native `gtn-record-screen.sh` capture running in the background
while several tool calls happen, with real thinking time landing in the
footage as dead air between them. When the interaction can't be pre-planned
into one script, collapse the dead air after the fact instead:

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-trim-pauses.sh --in /tmp/raw.mp4 --out /tmp/trimmed.mp4 --sensitivity medium
```

It runs ffmpeg's `mpdecimate` (drop frames that are near-duplicates of the
last kept frame) followed by `setpts` (re-time what's left back-to-back, so
the collapsed dead air doesn't leave a paused-then-jump-cut artifact). Verified
on a synthetic 5s-static + 3s-motion clip: output came back at 3.04s — the
static stretch collapsed to a blip, the motion survived essentially untouched.
`--sensitivity high` removes more (risks eating slow-but-real motion);
`--sensitivity low` removes less (leaves more residual dead air). This is
strictly a fallback — prefer the smooth-flow pattern above whenever the
interaction can be planned ahead of time, since it never records the dead air
rather than removing it afterwards.

### Recording something the app does on its own

For motion the agent is not driving — a flow executing, a spinner, streaming
output — a passive recorder is fine, as long as nothing else is attached:

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-record.sh --secs 8 --fps 6 --out /tmp/demo.mp4
${CLAUDE_SKILL_DIR}/scripts/gtn-record.sh --target web --secs 8 --out /tmp/demo.mp4
```

Frame capture is slower than the nominal interval on a large window, so the
result is a mild time-lapse rather than true real-time. The `--target web` form
is written but has not been exercised against a signed-in session yet.

### Recording native window chrome

Both of the above capture page content only. For traffic lights/title bars,
file dialogs, or window switching, record the screen instead:

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-record-screen.sh --secs 10 --out /tmp/window.mp4
```

```powershell
pwsh ${CLAUDE_SKILL_DIR}/scripts/gtn-record-screen.ps1 -Secs 10 -Out C:\temp\window.mp4
```

**macOS**: needs Screen Recording permission for the terminal (System Settings
→ Privacy & Security → Screen Recording). The script probes for it and fails
with a clear message rather than writing a black video.

The first capture after granting that permission can also trigger a separate
macOS dialog — *"\<terminal\> is requesting to bypass the system private window
picker and directly access your screen and audio"*. That is a one-time consent
scoped to screen/audio access; only the user should answer it, never click it
on their behalf. It does not reappear once allowed.

`objc[...]: class 'NSKVONotifying_AVCaptureScreenInput' not linked into
application` on stderr is harmless framework noise, not a failure — ignore it
as long as the script prints an `out=` line.

**Linux**: tries Wayland (`wf-recorder`) first, then falls back to X11
(`ffmpeg x11grab`) if the session isn't Wayland or `wf-recorder` isn't
installed. `wf-recorder` only works on wlroots compositors (Sway, Hyprland,
river) — on GNOME/KDE Wayland, run from an X11/XWayland session instead. No
permission prompt to handle either way. Best-effort, not exercised against a
real install.

**Windows**: uses `ffmpeg`'s `gdigrab`. No permission prompt to handle. Best-effort,
not exercised against a real install.

## Storage: scratch vs permanent

`~/.dev-browser/tmp/` (where `saveScreenshot` writes) is **scratch space,
never archival**. Two things live there and they're cleaned up differently:

- **Frame sequences** from `gtn-record.sh` / the smooth-flow and storyboard
  patterns. `gtn-encode.sh` and `gtn-record.sh` already delete these after
  encoding (unless `--keep` is passed) — no action needed.
- **One-off inspection screenshots** taken inline while looking at something
  ("let me check what that looks like now"). These are *not* auto-cleaned by
  anything, and giving each one a unique name is exactly how this tmp
  directory silently accumulated 12MB / 26 files across one session before
  this was fixed. **Reuse a single fixed scratch filename for these (e.g.
  `gtn-scratch.png`), overwritten every time, instead of inventing a new name
  per check.** A screenshot only earns a unique, permanent name once it's an
  actual deliverable — and at that point it belongs in permanent storage, not
  here.

If scratch files accumulate anyway (stale unique names from before this
convention, or a one-off exception), sweep them:

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-clean-scratch.sh
```

Scoped to this skill's own `gtn*`/`gtnrec_*` prefixes — never touches other
tools' files sharing that same tmp directory.

**Permanent storage** for finished recordings/screenshots the user actually
wants to keep:

```
~/Documents/gtn-verify/recordings/   # mp4s, and any GIF made from one
~/Documents/gtn-verify/screenshots/  # still PNG/JPG only
```

The split is by *what it is*, not by file extension — a GIF made from a
recording is still a recording (motion, derived from an mp4, exists to show an
interaction), so it lives in `recordings/` next to its source mp4, e.g.
`2026-08-04-search-filter-demo.mp4` and
`2026-08-04-search-filter-demo-preview.gif` side by side. `screenshots/` is
for genuine single-frame stills only.

Not `~/GriptapeNodes/` — that's the live workspace the engine itself reads and
lists in its own UI file browser; dropping unrelated media in there risks
cluttering that view. Name files descriptively with a date, e.g.
`2026-08-04-corridorkey-connect.mp4`, and only copy a file there once — don't
default to writing every capture straight to permanent storage; capture to
scratch, review it, then copy over what's worth keeping.

## Showing results in chat

Screenshots (PNG/JPG) render inline automatically via `Read` — this is the
normal way to show a result, already used throughout this skill.

**Video does not render inline anywhere in Claude Code** — not the VS Code
panel, not `Read`, not Artifacts (Artifacts are CSP-locked to a degree that
rules out practical video sources). Confirmed by direct research against
Claude Code's own docs. An mp4 is a real, useful deliverable to hand the user
a path to, but it will not play back inside the conversation.

When the user needs to actually *see* motion in the chat itself (not just have
the file), convert to GIF — a plain image format, so it renders inline exactly
like a screenshot:

```bash
${CLAUDE_SKILL_DIR}/scripts/gtn-to-gif.sh --in /tmp/demo.mp4 --out /tmp/demo.gif
```

Then `Read` the GIF path. Verified this renders inline. Keep the source mp4 as
the actual deliverable (better quality, smaller file than the GIF) — the GIF
is a lossy preview for the conversation, not a replacement.

## Prefer engine state over UI clicks

Every running engine (including the one bundled inside the desktop app)
unconditionally starts a Streamable HTTP MCP server on `http://localhost:8125/mcp/`
— see `griptape-nodes-engine/src/griptape_nodes/retained_mode/managers/agent_manager.py`,
`on_app_initialization_complete`. It is registered in Claude Code as the
`griptape-nodes` MCP server (user scope — `claude mcp list` should show it
Connected). No auth; it's loopback-only and tied to whichever engine process
is currently running.

**If the tools aren't showing up in `ToolSearch`**, this session predates the
`claude mcp add` call — MCP tool lists load once at session start. Tell the
user a new session is needed to pick it up; don't try to work around it
mid-session. You can still exercise the server directly over HTTP in the
meantime (see `references/architecture.md` for the raw JSON-RPC handshake used
to verify this originally) — but that's a fallback, not the normal path.

Common tools, named after the engine's retained-mode request classes (call
signatures come from `tools/list` — inspect via `ToolSearch` once connected):

| Need | Tool |
|---|---|
| Add a node | `CreateNodeRequest` |
| Wire two nodes together | `CreateConnectionRequest` |
| Set a parameter's value | `SetParameterValueRequest` |
| List nodes in the current flow | `ListNodesInFlowRequest` |
| List a node's connections | `ListConnectionsForNodeRequest` |
| Full state of a node in one call | `GetAllNodeInfoRequest` |
| Several of the above in one round trip | `EventRequestBatch` (send a list of `{request_type, request}` pairs) |

Verified: `ListNodesInFlowRequest` correctly returned `["Load Video",
"CorridorKey Video Inference"]` and `ListConnectionsForNodeRequest` correctly
returned the `video → video` edge, both built moments earlier via UI clicks in
the desktop app — same engine, same state, two different doors into it.

Only fall back to constructing a graph via clicks/drags when the user
explicitly wants the UI interaction exercised. That path — quick-add-node-by-
`Tab`, disambiguating React Flow's duplicate source/target handles, connecting
ports without misusing iframe-local coordinates — is documented in
[references/architecture.md](references/architecture.md#building-a-graph-via-ui).

## Reporting

State what you saw in the screenshot, not that you took one. If the change is not
visible, say so plainly and show the evidence rather than assuming it worked.

Deeper detail on the architecture, ports and gotchas:
[references/architecture.md](references/architecture.md).
