---
name: inline-substitution
description: Reference guide for the inline {VAR} variable substitution system — architecture, data flow, key files, and extension points
---

# Inline `{VAR}` Variable Substitution

This document describes how the inline `{VAR}` substitution system works in the engine, so engineers can extend it or add substitution to new areas without re-deriving the architecture.

## Overview

Users write `{VAR_NAME}` tokens inside node parameter values. At node execution time, the engine resolves each token against the variables visible in scope. The template is always preserved in the UI; only the value flowing to downstream nodes and workers is substituted.

Variables live in three layers, resolved hierarchically with closer layers shadowing farther ones:

```
FLOW (starting flow → ancestor flows)   ← highest priority
PROJECT (computed names + stored project variables)
GLOBAL                                  ← lowest priority
```

## The stored/computed split (core architecture)

**Stored variables** are plain data owned by `VariablesManager`:
- **Flow layers** — `dict[flow_name, VariableLayer]`, user variables created via `CreateVariableRequest`
- **Global layer** — one `VariableLayer`, `is_global=True` creates
- **Project layers** — `dict[project_id, VariableLayer]`, populated from a project.yml `variables:` section at template load (`ProjectManager._install_project_variables` → `VariablesManager.set_project_variables`)

**Computed values** are NOT stored anywhere — they're a namespace owned by `ProjectManager`, resolved fresh from live context on every read:
- **Builtins**: `{workspace_dir}` (engine config), `{workflow_name}` / `{workflow_dir}` (live workflow context), `{project_dir}`, `{static_files_dir}`
- **Template directories**: any `directories:` entry in project.yml (e.g. `{outputs}`)

Why the split: per-object callbacks die at serialization, and eager values go stale (`{workflow_dir}` tracks the open workflow). So stored variables serialize as plain `FlowVariable` data, while computed values are pulled per-read via two narrow public methods on `ProjectManager`:
- `resolve_project_variable(name, *, project_id) -> FlowVariable` — READ_ONLY snapshot, raises on context-not-ready
- `project_computed_names(*, project_id) -> frozenset[str]` — the names, cached on `ProjectInfo` at load (names are stable per load; values are volatile)

Within the PROJECT tier, **computed wins over stored**: a stored project variable named like a builtin is unreachable (template load warns about the collision).

## Reserved names

A computed name (builtin or directory) is **reserved in every scope**: `CreateVariableRequest` and `RenameVariableRequest` refuse to create/rename ANY variable (flow or global) to a reserved name of the relevant project. For PROJECT-layer renames the reserved set comes from the variable's *own* project (`request.project_id`), not the current one. Stored project variables are NOT reserved.

## Writable project variables (project.yml `variables:`)

```yaml
variables:
  shot_code:
    value: sc042              # type defaults to "str", permission to read_write
  frame_start:
    value: 1001
    type: int                 # str | int only; strict — no bool/float coercion
  facility:
    value: mtl
    permission: read_only
```

- Schema: `ProjectVariableDef` (`common/project_templates/variable.py`), strict value types, declared-type/value agreement enforced.
- Parent/child merge: per-entry atomic; `name: null` tombstones an inherited entry.
- READ_WRITE entries accept runtime writes (`SetVariableValueRequest` etc.); writes are gated against the schema (str/int, value agrees with declared type) BEFORE mutating, then **eagerly persisted** back to project.yml via `ProjectManager.persist_project_variables` (direct file write — deliberately not through `SaveProjectTemplateRequest`, whose cache invalidation would evict the layer just written).
- PROJECT-tier *reads* return snapshot copies; the write paths re-fetch the live stored object (`_stored_project_variable_for_write`) so a permitted write mutates real state.
- Stored project variables also participate in **macro path resolution** (`GetPathForMacroRequest`) at precedence: builtins > directories > caller-supplied > stored project vars > project env > shell env.

## Key files

| File | Role |
|------|------|
| `src/griptape_nodes/retained_mode/variable_types.py` | `FlowVariable`, `VariableLayer` (storage primitive), `VariableScope` (search strategy), `VariableLayerKind` (provenance), `VariablePermission` |
| `src/griptape_nodes/retained_mode/managers/variable_manager.py` | All stored layers; hierarchical search with provenance; write gating (`_refuse_write` — permission-based); project write-through + persist calls |
| `src/griptape_nodes/retained_mode/managers/project_manager.py` | Computed namespace (`resolve_project_variable`, `project_computed_names`); `_install_project_variables` at load; `persist_project_variables`; macro handlers with `project_id` |
| `src/griptape_nodes/common/project_templates/variable.py` | `ProjectVariableDef` schema for project.yml `variables:` |
| `src/griptape_nodes/exe_types/variable_resolver.py` | Core substitution logic: regex detection, recursive string/dict/list walking, `aprocess_scope()` ContextVar cache |
| `src/griptape_nodes/common/node_executor.py` | Resolves variables on the orchestrator before dispatching `ExecuteNodeRequest`; passes them in `request.variables` |
| `src/griptape_nodes/retained_mode/managers/node_manager.py` | On the worker side, `on_execute_node_request` enters `aprocess_scope(request.variables)` before calling `aprocess()` |
| `src/griptape_nodes/exe_types/node_types.py` | `BaseNode.validate_before_workflow_run()` marks nodes with `{VAR}` in parameters as UNRESOLVED before each run |
| `src/griptape_nodes/retained_mode/events/variable_events.py` | The request surface (see decision table below) |

## Choosing the right variable request

| Event | Returns | Use when |
|---|---|---|
| `ListVariablesRequest` | `variables: list[FlowVariable]` + parallel `layers: list[VariableLayerKind]` | **The enumeration surface.** Execution-time `{VAR}` context AND frontend pickers — derive grouping from `layers[i]` (`project` → macro-ish) and read_only from `permission` |
| `GetVariablesRequest` | `variables: dict` (hits) + `unresolved: list[str]` (misses) | **Named probe**: "of THESE names, which resolve and to what?" A miss is data, not a failure. Ideal for `ParsedMacro.get_variables()` dry-runs. `names` required |
| `GetVariableRequest` / `GetVariableValueRequest` / `HasVariableRequest` | single variable / value / bool | Point lookups. The standard library's variable nodes speak these |

All read requests take `lookup_scope: VariableScope` (default HIERARCHICAL; also PROJECT_ONLY, GLOBAL_ONLY, HIERARCHICAL_FROM_PROJECT, CURRENT_FLOW_ONLY, ALL) and `project_id: str | None` (None = current project; an explicit id enables **hypothetical resolution** — "how would this resolve if I were on project Y?").

**DEPRECATED — do not add callers** (deletion tracked in engine issue #5143; GUI migration in griptape-vsl-gui#2668):
- `ResolveSubstitutionRequest` — superseded by `ListVariablesRequest` (build the dict from `result.variables`)
- `ListSubstitutablesRequest` / `Substitutable` — superseded by `ListVariablesRequest` + `layers`
- `SetVariablesRequest` — batch write, zero senders; frozen at pre-#5142 behavior (refuses PROJECT-layer entries)

## Hypothetical resolution (`project_id`)

Six project requests take `project_id: str | None = None`: `GetPathForMacroRequest`, `GetStateForMacroRequest`, `AttemptMatchPathAgainstMacroRequest`, `GetSituationRequest`, `GetAllSituationsForProjectRequest`, `AttemptMapAbsolutePathToProjectRequest` — plus every variable read. `None` = current project; a loaded-but-not-current id answers "how WOULD this resolve on that project?" Caveat: `workflow_name`/`workflow_dir` always come from the *live* workflow context and `workspace_dir` from engine config, regardless of `project_id` — the hypothetical swaps the project, not the open workflow.

## Data flow

```
CreateVariable / SetVariableValue
        │
        ▼
VariablesManager stores FlowVariable (project writes → gate → persist to project.yml)
        │
        ▼  (dirty tracking)
_unresolve_nodes_referencing_variables()
  → ObjectManager: find nodes with {VAR} in params
  → make_node_unresolved() + unresolve_future_nodes()
        │
        ▼  (pre-run hook)
BaseNode.validate_before_workflow_run()
  → marks nodes with {VAR} params UNRESOLVED
  → ensures they re-enter the execution queue
        │
        ▼
NodeExecutor._resolve_variables_for_node(node_name)
  → ListVariablesRequest(starting_flow=..., lookup_scope=HIERARCHICAL)
  → {v.name: v.value for v in result.variables}
  → VariableResolver._filter_for_substitution(...)  # str/int only, no bool
  → returns dict[str, str | int]
        │
        ▼
ExecuteNodeRequest(variables=<dict>)   ← orchestrator passes to worker
        │
        ▼
node_manager.on_execute_node_request()
  → with aprocess_scope(request.variables):
        │
        ▼
BaseNode.aprocess()
  → get_parameter_value() calls VariableResolver.substitute()
  → TrackedParameterOutputValues.__setitem__ calls _resolve_variables_in_value()
```

During the hierarchical walk, the PROJECT tier resolves computed names fresh (`resolve_project_variable`); a computed value whose context isn't ready (e.g. `{workflow_dir}` before the workflow is saved) is silently skipped with **no stored-layer fallback** — the name exists, it's just unavailable.

## Display preservation

The UI must always show the template the user typed (`{SHOT}`), not the resolved value (`25`). Each of the following push paths would otherwise overwrite the display; each suppresses via `BaseNode.get_display_value_for_output()`. The list is the set of paths known and fixed, **not** a proof of completeness — a new emit site is a new leak until it goes through the guard.

1. **During execution** (`TrackedParameterOutputValues.__setitem__`): fires `AlterElementEvent` with the template, not the resolved value.
2. **After execution** (`parallel_resolution.py` `handle_done_nodes`): `ParameterValueUpdateEvent` and `NodeResolvedEvent` both apply `get_display_value_for_output()` before serialising.
3. **Browser refresh / reload** (`node_manager._set_param_to_value`): applies `get_display_value_for_output()` when building `element_id_to_value` from `parameter_output_values`.
4. **Dict/list parameters** (JSON Input): `TrackedParameterOutputValues.__setitem__` calls `_resolve_variables_in_value()` inside `aprocess_scope()` so downstream nodes get the substituted dict while `parameter_values` and the UI keep the template.
5. **Mid-execution progress updates** (`BaseNode.publish_update_to_parameter`): suppresses before building `ParameterValueUpdateEvent`. This method also writes `parameter_output_values`, so one call emits two events; without suppression the second overwrites the template the first just set.
6. **Streaming appends** (`BaseNode.append_value_to_parameter`): returns before emitting its `ProgressEvent` when the accumulated output would replace a template. `ProgressEvent` goes onto the event queue **bare**, not wrapped in `ExecutionGriptapeNodeEvent` — worth knowing when writing tests that inspect captured events.
7. **Group / worker copy-back** (`node_executor.execute`): writes land in `parameter_output_values` **after** `aprocess_scope` has exited, so the `__setitem__` guard must not be gated on `_in_aprocess`.

`get_display_value_for_output()` returns the stored template when all of:
- substitution is enabled (`VariableResolver.is_substitution_enabled()`), AND
- the parameter exists and has `ParameterMode.PROPERTY` in its allowed modes, AND
- the parameter has `allow_variable_substitution=True`, AND
- the stored value contains a variable macro, AND
- the resolved output differs from the stored value, AND
- the parameter has no incoming connection (a connected value is upstream data, not a template the user typed)

These mirror the gate in `get_parameter_value`: there is only a template worth preserving where substitution would actually have replaced it. The check is read-only — it never modifies `parameter_output_values`.

**Deliberate exception — `GetParameterValueRequest` returns the resolved value.** It is a data read, not a UI push: the scripting API (`retained_mode.py`), the MCP server (`mcp.py`), and `node_manager`'s connection-source lookup all read through it and need the value that actually flowed. Suppressing there would hand callers the template string as if it were data.

### Never write a resolved value into `parameter_values`

Display suppression reads the template out of `parameter_values`. A caller that copies execution results back onto a node and routes them through `set_parameter_value` (or `SetParameterValueRequest`) destroys that template, and the loss is permanent rather than cosmetic: a browser refresh cannot recover it, and the next save persists the substituted string.

Copy-back sites guard with `BaseNode.should_preserve_stored_template()` and write to `parameter_output_values` only:

```python
if not target_node.should_preserve_stored_template(target_param_name, param_value):
    target_node.set_parameter_value(target_param_name, param_value)
target_node.parameter_output_values[target_param_name] = param_value
```

Both `should_preserve_stored_template()` and `get_display_value_for_output()` delegate to `_variable_template_to_preserve()`, so the predicate and the display value cannot drift apart. Current call sites: `_apply_last_iteration_to_packaged_nodes` (parallel-loop path) and `_apply_parameter_values_to_node` (sequential group / subflow path).

Skipping the write also skips everything `set_parameter_value` does on the way through: converters and validators, `before_value_set` / `after_value_set` hooks, container-parent recomputation for `ParameterList`/`ParameterDictionary` children, and `unresolve_future_nodes()`. Only the last matters here — the others operate on stored state that is intentionally left alone — so `_apply_parameter_values_to_node` calls `_unresolve_future_nodes_for_skipped_write()` on the skip branch to keep downstream invalidation intact. (`_apply_last_iteration_to_packaged_nodes` never invalidated downstream in the first place; it calls `set_parameter_value` directly, outside the request handler.)

## Worker-side variable seeding

Workers receive transient nodes that are never registered in `ObjectManager`. This means:
- `get_node_parent_flow_by_name()` raises `KeyError` on workers
- The lazy "fetch variables from the engine" path fails silently

**Fix**: orchestrators resolve variables before dispatching. `_resolve_variables_for_node` runs on the orchestrator (where the node IS in `ObjectManager`) and passes the resolved dict into `ExecuteNodeRequest.variables`. Workers receive a fully populated dict; they never need to fetch. (`ListVariablesRequest` is also in `worker_routing.FORWARDED_REQUEST_TYPES` for the fallback path.)

Critical: `aprocess_scope(request.variables)` — **not** `aprocess_scope(request.variables or None)`. An empty dict (`{}`) means "substitution enabled, no variables defined." `or None` converts that to `None`, which re-triggers the broken lazy fetch path.

## Dirty tracking

Nodes are marked UNRESOLVED (so they re-run) when:
1. A variable value changes at edit time → `_unresolve_nodes_referencing_variables()` walks all nodes
2. Before each flow run → `validate_before_workflow_run()` catches any nodes that became resolved between edits

Runtime re-queuing is not possible: the flow engine populates the execution queue before execution starts; marking a node UNRESOLVED mid-run has no effect on the current run. The pre-run hook is the reliable interception point.
