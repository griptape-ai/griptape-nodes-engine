---
name: griptape-nodes-workflows
description: Build, run, and inspect Griptape Nodes workflows by driving the engine's MCP server. Use when the user asks to construct a node workflow, run an existing one, set parameter values, wire connections between nodes, or read output from a workflow run. Triggers include "build a workflow", "add a node to the flow", "connect these nodes", "run the flow", "what does node X output".
---

# Griptape Nodes Workflow Construction Guide

This skill covers the full cold-start cycle (build → wire → run → read) against the engine's MCP server, plus the idioms and gotchas discovered from running real workflows.

## Mental Model

- **Workflow**: Top-level namespace. Only ONE can be active at a time. Reset with `ClearAllObjectStateRequest`.
- **Flow**: The canvas inside a workflow. A workflow has exactly one top-level "canvas" flow. Sub-flows are possible but rarely needed for scratch work.
- **Node**: A unit of work with parameters (inputs, outputs, properties).
- **Connection**: An edge between two parameters. Two kinds:
    - **Data flow**: A typed parameter on one node → a typed parameter on another. The engine derives execution order from data dependencies, so data connections alone are usually sufficient.
    - **Control flow**: `exec_out` → `exec_in`. Only needed when two nodes must run in a specific order but don't share data (e.g. side-effecting steps, branching). Skip these by default.
- **Current Context**: A stack (workflow → flow → node). Most requests default to "current" when a name is omitted.

## What the MCP server actually exposes

Every MCP tool corresponds 1:1 to a `RequestPayload` class registered in
`SUPPORTED_REQUEST_EVENTS` (`src/griptape_nodes/servers/mcp.py`). The server name is
prefixed onto each tool, so the request `CreateNodeRequest` is reachable as
`griptape_nodes_CreateNodeRequest`. A few consequences worth remembering up front:

- **There are no plural variants of individual requests.** `CreateNodesRequest`,
    `CreateConnectionsRequest`, etc. do not exist. To create N nodes, send N
    `CreateNodeRequest` calls — or wrap them in a single `EventRequestBatch` call
    (see below).
- **`CreateNodeRequest` does not take parameter values.** Setting a parameter is
    always a separate `SetParameterValueRequest` after the node exists. There is no
    `parameter_values` / `inputs` shortcut on create.
- **`EventRequestBatch` is the only fan-out primitive.** It is a synthetic tool
    (no matching `RequestPayload` class) that ships an ordered list of inner
    requests in one transport frame. Reach for it whenever you already know the
    shape of a build phase. See "EventRequestBatch: collapse a build phase into
    one round trip" below.

## Survey the Workspace First

Before building anything, find out what is actually loaded in this engine. The set of
registered libraries and the node types they expose is the catalog every later step
draws from, so spend the round trips up front instead of guessing names that may not
exist (or that exist in a different library than you expect).

### Find the workspace directory on disk

The MCP surface does not expose `GetConfigValueRequest`, so resolve the workspace
path by reading the user config file directly. On macOS / Linux it lives at:

```
~/.config/griptape_nodes/griptape_nodes_config.json
```

The keys you care about are:

- `workspace_directory`: absolute (or `~`-prefixed) workspace root. The sandbox
    library lives inside this directory.
- `app_events.on_app_initialization_complete.libraries_to_register`: list of locally
    registered `griptape_nodes_library.json` paths. Entries can be either a string or
    an object `{"path": "...", "enabled": true}`. Reading these tells you where each
    library's source lives if you need to look at an existing node's Python module
    before writing a similar sandbox node.

The sandbox subdirectory key (`sandbox_library_directory`) is optional and defaults
to `sandbox_library`. The absolute sandbox path is therefore
`<workspace_directory>/<sandbox_library_directory>` (e.g.
`~/Projects/.../GriptapeNodes/sandbox_library`).

Do this once per session and remember the paths; they don't change mid-run.

### Survey the registered libraries via MCP

```
A. griptape_nodes_ListRegisteredLibrariesRequest()
   → list of library names currently loaded (e.g. "Griptape Nodes Library",
     "Sandbox Library"). If a library you expect is missing, no `DescribeNodeType`
     call against its node types will resolve.

B. griptape_nodes_ListNodeTypesInLibraryRequest(library="<name>")
   → call once per library you might pull from. The returned node-type names are the
     exact strings to pass to `CreateNodeRequest.node_type` and
     `DescribeNodeTypeRequest.node_type`.

C. (optional) griptape_nodes_ListCategoriesInLibraryRequest(library="<name>")
   → useful when you only care about a slice of a large library (e.g. only image
     nodes) and want to scope the next step.
```

Do this once per session, then reuse the catalog. Skip the survey only when you
already know (from a prior call in the same session) which library provides each node
type you intend to use.

## EventRequestBatch: collapse a build phase into one round trip

`EventRequestBatch` (MCP tool name: `griptape_nodes_EventRequestBatch`) wraps an
ordered list of inner requests in a single transport frame. Use it whenever the
shape of the build phase is already decided — typical pattern is N `CreateNodeRequest`

- M `SetParameterValueRequest` + K `CreateConnectionRequest` + one
    `AutoLayoutFlowRequest`, all in one round trip.

Shape:

```json
{
  "requests": [
    {"request_type": "CreateNodeRequest",
     "request": {"node_type": "TextInput", "node_name": "TextInput_1"}},
    {"request_type": "SetParameterValueRequest",
     "request": {"node_name": "TextInput_1", "parameter_name": "text", "value": "..."}}
  ],
  "timeout_ms": 60000
}
```

Behavior:

- **Sequential dispatch.** The engine awaits inner requests in submission order
    (`for inner in batch.requests: await _dispatch_event_request(inner)`), so a
    `CreateNodeRequest` followed by a `SetParameterValueRequest` on that same node
    is safe inside one batch.
- **Pre-flight validation.** Every inner request is constructed against its
    `RequestPayload` class before anything goes on the wire. An unknown
    `request_type`, a `request` that is not a JSON object, or unknown kwargs in the
    inner payload all reject the entire batch up front with a `TypeError` /
    `ValueError`.
- **Per-slot failure isolation.** Once the batch is dispatched, a failure in one
    slot does not abort siblings. Failed slots come back as
    `{"ok": false, "details": "..."}` in the result array; successful slots come
    back as the same flattened object a single tool call would return. Walk every
    slot before assuming the batch succeeded.
- **No nesting.** `EventRequestBatch` is intentionally absent from
    `SUPPORTED_REQUEST_EVENTS` and the inner `request_type` enum, so a batch cannot
    contain another batch.
- **Default timeout scales with size.** `timeout_ms` defaults to
    `30000 × len(requests)` clamped at `300000` ms (5 min). Pass an explicit
    override when the last slot is `StartFlowRequest(wait_for_completion=True)` or
    any other long-running call; otherwise the synchronous run can eat the budget
    meant for the rest of the batch. `bool` is rejected explicitly so `True`
    cannot silently become 1ms.

Return shape: a JSON array of trimmed slot responses in submission order. Each slot
looks identical to the response that single-tool dispatch would have returned for
that `request_type`.

```json
[
  {"ok": true, "node_name": "TextInput_1", "...": "..."},
  {"ok": true, "finalized_value": "...", "...": "..."}
]
```

### Critical idiom: pre-name nodes you reference later in the same batch

Inside one batch you cannot read `CreateNodeResultSuccess.node_name` from an
earlier slot before composing a later one — every entry is fixed when the batch is
submitted. So either:

- Pass an explicit `node_name` on every `CreateNodeRequest` and reuse those names
    verbatim in later `SetParameterValueRequest` / `CreateConnectionRequest`
    entries, or
- Split the build across two batches: one that creates nodes (read the assigned
    names back from the result array), then a second that sets parameters and
    wires them up.

The one-batch + explicit names path is almost always shorter and is what the
batched recipe below uses.

## Canonical Cold-Start Recipe

For a typical 3-node linear pipeline (`TextInput → Agent → DisplayText`), once the
workspace survey above has confirmed the relevant library exposes those node types:

```
1. griptape_nodes_EnsureWorkflowAndFlowRequest()
   → returns workflow_name, flow_name, created_workflow, created_flow.
     Idempotent: if both pieces are already in context, reuses them.

2. griptape_nodes_DescribeNodeTypeRequest(node_type="TextInput")
3. griptape_nodes_DescribeNodeTypeRequest(node_type="Agent")
4. griptape_nodes_DescribeNodeTypeRequest(node_type="DisplayText")
   → get exact parameter names/types/modes. Look for:
     - data input(s)  (mode_allowed_input  == true)
     - data output(s) (mode_allowed_output == true)
     - control params (type == "parametercontroltype"): exec_in / exec_out
       are usually skippable (see Mental Model)

5. griptape_nodes_CreateNodeRequest(node_type="TextInput")
6. griptape_nodes_CreateNodeRequest(node_type="Agent")
7. griptape_nodes_CreateNodeRequest(node_type="DisplayText")
   → each returns a flat `node_name` (e.g. "TextInput_1", "Agent_1"). Read it from
     the response, do not assume a naming convention. The default name comes from
     `metadata.display_name` and may include spaces (e.g. "Text Input_1"). Pass an
     explicit `node_name` if you want a stable handle.

8. griptape_nodes_SetParameterValueRequest(
       node_name="TextInput_1", parameter_name="text", value="...")
   → there is no `parameter_values` shortcut on CreateNode; set every non-default
     parameter with its own SetParameterValue call.

9. griptape_nodes_CreateConnectionRequest(
       source_node_name="TextInput_1",   source_parameter_name="text",
       target_node_name="Agent_1",       target_parameter_name="prompt")
10. griptape_nodes_CreateConnectionRequest(
       source_node_name="Agent_1",       source_parameter_name="output",
       target_node_name="DisplayText_1", target_parameter_name="text")
    → data connections only; the engine orders execution from data dependencies.

11. griptape_nodes_AutoLayoutFlowRequest()
    → required after any multi-node build. Without it, every node lands at (0, 0)
      and the canvas shows them stacked on top of each other. Topologically sorts
      the graph and assigns column-and-row positions. Omit `flow_name` to lay out
      the current-context flow.

12. griptape_nodes_StartFlowRequest(wait_for_completion=True, completion_timeout_ms=60000)
    → omit flow_name; the handler uses the current-context flow.
      wait_for_completion blocks until the flow resolves or times out.

13. griptape_nodes_GetParameterValueRequest(node_name="DisplayText_1", parameter_name="text")
    → the terminal node's output.
```

Thirteen MCP calls: 1 ensure + 3 describe + 3 create + 1 set + 2 connect + 1 layout +
1 run + 1 read. Wider graphs scale linearly: every node adds one CreateNode plus its
SetParameterValue calls; every edge adds one CreateConnection.

### Batched variant (4 round trips)

The build phase (steps 5-11 above) has fixed shape, so it collapses into one
`EventRequestBatch` call. The describe phase still benefits from being a separate
batch because its results inform the build payload, and `StartFlowRequest` is
usually kept out of the build batch so its long timeout does not gate the rest:

```
1. EnsureWorkflowAndFlowRequest                        (1 call)
2. EventRequestBatch([                                  (1 call, runs sequentially)
     DescribeNodeTypeRequest("TextInput"),
     DescribeNodeTypeRequest("Agent"),
     DescribeNodeTypeRequest("DisplayText"),
   ])
3. EventRequestBatch([                                  (1 call, runs sequentially)
     CreateNodeRequest(node_type="TextInput",   node_name="TextInput_1"),
     CreateNodeRequest(node_type="Agent",       node_name="Agent_1"),
     CreateNodeRequest(node_type="DisplayText", node_name="DisplayText_1"),
     SetParameterValueRequest(node_name="TextInput_1", parameter_name="text", value="..."),
     CreateConnectionRequest(source_node_name="TextInput_1",   source_parameter_name="text",
                             target_node_name="Agent_1",       target_parameter_name="prompt"),
     CreateConnectionRequest(source_node_name="Agent_1",       source_parameter_name="output",
                             target_node_name="DisplayText_1", target_parameter_name="text"),
     AutoLayoutFlowRequest(),
   ])
4. StartFlowRequest(wait_for_completion=True, completion_timeout_ms=60000)
   + GetParameterValueRequest("DisplayText_1", "text")            (2 calls)
```

Four round trips instead of thirteen. Walk the result arrays after each batch and
verify every slot returned `ok: true` before moving on; per-slot failures don't
abort the rest of the batch, so a typo in slot 4 still lets slots 5 and 6 run
against stale state.

## Key Idioms

- **Survey the workspace before picking node types.** Read the JSON config at
    `~/.config/griptape_nodes/griptape_nodes_config.json` to learn where the sandbox
    directory and registered library JSONs live, then run
    `ListRegisteredLibrariesRequest` and `ListNodeTypesInLibraryRequest` for the
    libraries you care about before reaching for `DescribeNodeType`. The catalog tells
    you which node types actually exist in this engine and which library owns each
    one, which is the input both `DescribeNodeTypeRequest.library` and
    `CreateNodeRequest.specific_library_name` expect when the same name lives in more
    than one library.
- **Discover before wiring.** Always call `DescribeNodeType` for each node type you
    intend to use before guessing parameter names. The cost is 3-5 calls up front but
    saves many round trips fighting typos and assumed-wrong names.
- **Batch with `EventRequestBatch` once the shape is known.** Anything past the
    discovery phase usually has fixed shape (N creates + M sets + K connects + a
    layout). Wrap them in one `EventRequestBatch` call and pre-name every node you
    reference later in the same batch. Inspect every slot of the result array;
    per-slot failures do not abort siblings. Keep `StartFlowRequest` out of the
    build batch unless you raise `timeout_ms` to cover the synchronous run.
- **Wire data, not control.** The engine derives execution order from data
    dependencies, so `exec_out` → `exec_in` connections are usually noise. Only add
    them when two nodes must run in a specific order but don't exchange data.
- **Always run AutoLayout after a multi-node build.** Without it nodes land at
    (0, 0) and stack on top of each other. `AutoLayoutFlowRequest` is one round trip
    and idempotent; treat it as the closing step of any build phase.
- **Use `wait_for_completion=True` on `StartFlowRequest`.** For workflows that touch
    LLMs, image generators, or long I/O, set `completion_timeout_ms` generously
    (60000+ ms). Otherwise the call returns the instant the flow is kicked off and
    you have to poll `GetNodeResolutionStateRequest` yourself.
- **Omit `flow_name` on `StartFlowRequest`** when you just finished building a
    single flow. The handler defaults to the current-context flow.
- **Read the response, don't assume names.** `CreateNodeResultSuccess.node_name` is
    the authoritative handle for every later call. Pass it verbatim into
    `SetParameterValueRequest`, `CreateConnectionRequest`, and
    `GetParameterValueRequest`.

## Response Shape

Every MCP tool returns a trimmed object (the engine envelope is unwrapped server-side
in `_trim_response`):

```json
{
  "ok": true,
  "details": "<human-readable summary, may be omitted>",
  "altered_workflow_state": true|false,
  "...": "...payload fields..."
}
```

`ok` reflects whether the engine produced a `*ResultSuccess` payload. The remaining
fields are flattened from the inner result class, so for example
`CreateNodeResultSuccess.node_name` is reachable as `response["node_name"]`,
`EnsureWorkflowAndFlowResultSuccess` exposes `workflow_name`, `flow_name`,
`created_workflow`, `created_flow` at the top level, and `AutoLayoutFlowResultSuccess`
exposes `flow_name` and `positioned_nodes`. Failures surface as MCP tool errors with
the engine's `result_details` message attached.

`EventRequestBatch` returns a JSON **array** instead of a single object. Each slot
mirrors the single-call shape above, so a batched build phase looks like
`[{"ok": true, "node_name": "TextInput_1", ...}, {"ok": true, ...}, ...]`. Failed
slots come back as `{"ok": false, "details": "..."}` in place; the rest of the array
still executes.

## Gotchas

### DescribeNodeType may be partial

`DescribeNodeType` probes by instantiating the node class. For node types whose
`__init__` performs I/O (network, auth, disk), instantiation may fail. When that
happens the response still succeeds but returns:

- Full library-level `metadata` (category, description, display_name, etc.)
- `parameters: []` (empty — instantiation failed before parameters were declared)
- A WARNING-level entry in `details` naming the cause

You still know what the node does, but you cannot see its parameter schema from MCP
alone. Consider falling back to a different node type, or describe on a system where
credentials are present.

### CreateNode can silently produce an ErrorProxyNode

When a node fails to instantiate and `create_error_proxy_on_failure=True` (the
default), the engine substitutes an `ErrorProxyNode` and reports success. Inspect the
response's `node_type` (it will be the proxy class) or the `details` string if a
later step fails mysteriously. If you need strict failure semantics, set
`create_error_proxy_on_failure=False` on the request.

### Default node names contain spaces

The engine names nodes after `metadata.display_name`, which is often human-readable
with spaces (e.g. `"Text Input_1"`, `"Display Text_1"`). That works for every API,
but it's easy to typo. Either pass an explicit `node_name` per request, or always
read the returned `node_name` from `CreateNodeResultSuccess` and reuse it verbatim.

### Connection request field names are long

`CreateConnectionRequest` uses the unabbreviated names
`source_node_name`, `source_parameter_name`, `target_node_name`,
`target_parameter_name`. There are no short aliases (`source`, `source_param`, etc.).
A typo here surfaces as an opaque validation error from pydantic, not a friendly
"unknown field" message.

### Only one workflow in context at a time

`SetWorkflowContextRequest` refuses if a workflow is already in context. To swap,
`ClearAllObjectStateRequest(i_know_what_im_doing=True)` first — this wipes
EVERYTHING (nodes, flows, connections, workflow). There is no softer reset today.

### Agents cannot be interrupted mid-run

There is no pause/cancel for a running flow today. Use `completion_timeout_ms` to
bound the wait; if the timeout fires, `StartFlowRequest` returns a failure but the
flow keeps running in the engine until it finishes or errors. A subsequent
`StartFlowRequest` will fail with "Flow is already running" until it does.

### ParameterList inputs start empty — add a child slot before setting or connecting

A `ParameterList` parameter (e.g. `items` on `CreateImageList` and the other
create-list nodes, or `input_images` on image-generation nodes) does **not** come
with any item slots. Its value is *derived* by collecting each child parameter's
value in order, so with zero children the list is empty no matter what you do to the
container. You cannot populate it by setting a value on the container parameter
directly, and there are no index-style child names (`items_0`, `items[0]`) to target.

To put one item into a `ParameterList`, add a child slot first, then act on the
**name the engine hands back**:

```
1. griptape_nodes_AddParameterToNodeRequest(
       node_name="CreateImageList_1", parent_container_name="items")
   → the engine creates a child slot and returns it in `parameter_name`, e.g.
     "items_ParameterListUniqueParamID_3f2a...". Do NOT pass parameter_name / type /
     tooltip here — for the container case they're ignored; the slot is cloned from
     the list. Do NOT invent the name; read it from the response.

2. griptape_nodes_SetParameterValueRequest(
       node_name="CreateImageList_1", parameter_name="<name from step 1>", value=...)
   — or —
   griptape_nodes_CreateConnectionRequest(
       source_node_name="...", source_parameter_name="...",
       target_node_name="CreateImageList_1", target_parameter_name="<name from step 1>")
```

Repeat step 1 for each item you want (once per slot). `parent_container_name` is
required — without it the request creates a plain sibling parameter on the node
instead of a list child.

**Batching caveat:** the child's name only exists *after* the add, so the
`AddParameterToNodeRequest` must be in an **earlier round trip** than the
`SetParameterValue` / `CreateConnection` that references it. It cannot be collapsed
into the same `EventRequestBatch` as the calls that use its returned name — do the
adds in one batch, read the names out of the result array, then do the sets/connects
in the next batch.

## Tool Cheat Sheet

| Goal                                                               | Tool                                                                                                                                                                       |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bootstrap a workflow + flow from cold                              | `EnsureWorkflowAndFlowRequest`                                                                                                                                             |
| Fan N requests out in one round trip                               | `EventRequestBatch` (synthetic; pre-name nodes that later slots reference)                                                                                                 |
| Discover libraries / node types                                    | `ListRegisteredLibrariesRequest`, `ListNodeTypesInLibraryRequest`, `ListCategoriesInLibraryRequest`                                                                        |
| Inspect a node type's parameters                                   | `DescribeNodeTypeRequest`                                                                                                                                                  |
| Create a node                                                      | `CreateNodeRequest`                                                                                                                                                        |
| Wire a single edge                                                 | `CreateConnectionRequest`                                                                                                                                                  |
| Lay out the canvas after a multi-node build                        | `AutoLayoutFlowRequest`                                                                                                                                                    |
| Move a single node to an explicit position                         | `SetNodeMetadataRequest` (set `metadata.position`)                                                                                                                         |
| Set a parameter value                                              | `SetParameterValueRequest`                                                                                                                                                 |
| Add an item slot to a ParameterList (e.g. `items`, `input_images`) | `AddParameterToNodeRequest(parent_container_name="<list>")` — returns the generated child name; set/connect against THAT name, not the container or an index (see Gotchas) |
| Read a parameter value                                             | `GetParameterValueRequest`                                                                                                                                                 |
| Inspect a parameter's schema/details on a live node                | `GetParameterDetailsRequest`, `ListParametersOnNodeRequest`                                                                                                                |
| Run synchronously                                                  | `StartFlowRequest(wait_for_completion=True, completion_timeout_ms=...)`                                                                                                    |
| Run from a specific node                                           | `StartFlowFromNodeRequest`                                                                                                                                                 |
| Resolve a single node without firing the control flow              | `ResolveNodeRequest`                                                                                                                                                       |
| Execute a single node directly                                     | `ExecuteNodeRequest`                                                                                                                                                       |
| Rename a node or flow                                              | `RenameObjectRequest(allow_next_closest_name_available=True)`                                                                                                              |
| Lock or unlock a node                                              | `SetLockNodeStateRequest`                                                                                                                                                  |
| Reset a node's parameters to defaults                              | `ResetNodeToDefaultsRequest`                                                                                                                                               |
| Inspect state                                                      | `ListNodesInFlowRequest`, `ListConnectionsForNodeRequest`, `GetNodeResolutionStateRequest`, `GetNodeMetadataRequest`, `GetConnectionsForParameterRequest`                  |
| Find nodes by Python class (e.g. StartFlow, Agent)                 | `ListNodesInFlowRequest(node_types=["StartFlow", "Agent"])` — returns only nodes whose class name matches; omit to get all nodes                                           |
| Register a sandbox node type from Python source already on disk    | `RegisterSandboxNodeFromSourceRequest` (see Custom nodes below)                                                                                                            |
| Reset everything                                                   | `ClearAllObjectStateRequest(i_know_what_im_doing=True)`                                                                                                                    |

## Custom nodes

If the task involves writing a new node type via `RegisterSandboxNodeFromSourceRequest`, read the [comprehensive node development guide](https://docs.griptapenodes.com/en/stable/development/custom_nodes/comprehensive_guide/index.md) **before** drafting source. The guide documents the engine-side conventions a sandbox class must follow:

- `BaseNode` subclassing and the `process` / `aprocess` contract
- `Parameter` declaration, modes (`mode_allowed_input` / `..._property` / `..._output`), traits
- `ParameterString` / `ParameterImage` / etc. helpers (preferred over hand-rolled `Parameter`)
- `ParameterGroup` / `ParameterList` containers
- Connection rules and node states

`RegisterSandboxNodeFromSourceRequest` only **registers** Python source already on
disk inside the sandbox library directory; it never writes the file itself. The
agent is responsible for placing the `.py` file under
`<workspace_directory>/<sandbox_library_directory>` (via its own filesystem tool,
e.g. pi's `write`) before issuing the request. The imported source then runs inside
the engine process with no isolation, so matching the conventions up front is faster
than iterating on registration failures. For pure workflow-driving tasks (build →
wire → run → read) the guide is overkill — stick to this skill.

## Example: One-Shot Haiku Pipeline

Goal: run an `Agent` on a one-line prompt and read the output.

1. `EnsureWorkflowAndFlowRequest()`
1. `DescribeNodeTypeRequest(node_type="TextInput")` → text output parameter is `text`
1. `DescribeNodeTypeRequest(node_type="Agent")` → input `prompt`; output `output`
1. `DescribeNodeTypeRequest(node_type="DisplayText")` → input `text`
1. `CreateNodeRequest(node_type="TextInput")` → read assigned `node_name`
1. `CreateNodeRequest(node_type="Agent")` → read assigned `node_name`
1. `CreateNodeRequest(node_type="DisplayText")` → read assigned `node_name`
1. `SetParameterValueRequest(node_name="TextInput_1", parameter_name="text", value="Write a haiku about clouds.")`
1. `CreateConnectionRequest(TextInput_1.text → Agent_1.prompt)`
1. `CreateConnectionRequest(Agent_1.output → DisplayText_1.text)`
1. `AutoLayoutFlowRequest()` → arrange the 3 nodes across columns
1. `StartFlowRequest(wait_for_completion=True, completion_timeout_ms=60000)`
1. `GetParameterValueRequest(node_name="DisplayText_1", parameter_name="text")`

Total: 13 MCP calls from empty engine to rendered output.

### Same pipeline, batched (4 round trips)

1. `EnsureWorkflowAndFlowRequest()`
1. `EventRequestBatch([DescribeNodeTypeRequest × 3])`
1. `EventRequestBatch([CreateNodeRequest × 3 (with explicit node_name), SetParameterValueRequest, CreateConnectionRequest × 2, AutoLayoutFlowRequest])`
1. `StartFlowRequest(wait_for_completion=True, completion_timeout_ms=60000)` then `GetParameterValueRequest("DisplayText_1", "text")`

The build batch in step 3 only works because every `CreateNodeRequest` carries an
explicit `node_name`; the later `SetParameterValueRequest` and
`CreateConnectionRequest` slots reference those names directly instead of waiting on
the per-create response.

## Further reading

For anything beyond the build → wire → run → read scope of this skill, start at
[`/for_agents/`](https://docs.griptapenodes.com/en/stable/for_agents/index.md). It is
the canonical entry point to the engine's machine-readable doc surface — it explains
the trade-offs between `/llms.txt` (curated index), `/llms-full.txt` (full corpus in
one fetch), and per-page `.md` files, and lists the highest-value pages for grounding
an agent in the engine's actual API.
