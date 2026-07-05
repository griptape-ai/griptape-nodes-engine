---
name: inline-substitution
description: Reference guide for the inline {VAR} variable substitution system — architecture, data flow, key files, and extension points
---

# Inline `{VAR}` Variable Substitution

This document describes how the inline `{VAR}` substitution system works in the engine, so engineers can extend it or add substitution to new areas without re-deriving the architecture.

## Overview

Users write `{VAR_NAME}` tokens inside node parameter values. At node execution time, the engine resolves each token against the user-defined workflow variables in scope. The template is always preserved in the UI; only the value flowing to downstream nodes and workers is substituted.

## Key files

| File | Role |
|------|------|
| `src/griptape_nodes/exe_types/variable_resolver.py` | Core substitution logic: regex detection, recursive string/dict/list walking, `aprocess_scope()` ContextVar cache |
| `src/griptape_nodes/common/node_executor.py` | Resolves variables on the orchestrator before dispatching `ExecuteNodeRequest`; passes them in `request.variables` |
| `src/griptape_nodes/retained_mode/managers/node_manager.py` | On the worker side, `on_execute_node_request` enters `aprocess_scope(request.variables)` before calling `aprocess()` |
| `src/griptape_nodes/retained_mode/managers/variable_manager.py` | Handles `SetVariableValueRequest` / `SetVariablesRequest` and calls `_unresolve_nodes_referencing_variables()` for dirty tracking |
| `src/griptape_nodes/exe_types/node_types.py` | `BaseNode.validate_before_workflow_run()` marks nodes with `{VAR}` in parameters as UNRESOLVED before each run |
| `src/griptape_nodes/retained_mode/events/variable_events.py` | `ResolveSubstitutionRequest` (supports `names=[]` for "all" or `names=[...]` for targeted lookup), `ResolveSubstitutionResultSuccess`; `ListSubstitutablesRequest` / `Substitutable` for frontend pickers |

## Data flow

```
CreateVariable / SetVariableValue
        │
        ▼
VariableManager stores FlowVariable
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
  → ResolveSubstitutionRequest(starting_flow=..., lookup_scope=HIERARCHICAL)
  → VariableResolver._filter_for_substitution(variables)
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

## Display preservation

The UI must always show the template the user typed (`{SHOT}`), not the resolved value (`25`). Four code paths can overwrite the display — all are suppressed via `BaseNode.get_display_value_for_output()`:

1. **During execution** (`TrackedParameterOutputValues.__setitem__`): fires `AlterElementEvent` with the template, not the resolved value.
2. **After execution** (`parallel_resolution.py` `handle_done_nodes`): `ParameterValueUpdateEvent` and `NodeResolvedEvent` both apply `get_display_value_for_output()` before serialising.
3. **Browser refresh / reload** (`node_manager._set_param_to_value`): applies `get_display_value_for_output()` when building `element_id_to_value` from `parameter_output_values`.
4. **Dict/list parameters** (JSON Input): `TrackedParameterOutputValues.__setitem__` calls `_resolve_variables_in_value()` inside `aprocess_scope()` so downstream nodes get the substituted dict while `parameter_values` and the UI keep the template.

`get_display_value_for_output()` returns the stored template when:
- parameter has `ParameterMode.PROPERTY` in its allowed modes, AND
- the stored template contains a variable macro, AND
- the resolved output differs from the template

It is read-only — it never modifies `parameter_output_values`.

## Worker-side variable seeding

Workers receive transient nodes that are never registered in `ObjectManager`. This means:
- `get_node_parent_flow_by_name()` raises `KeyError` on workers
- The lazy "fetch variables from the engine" path fails silently

**Fix**: orchestrators resolve variables before dispatching. `_resolve_variables_for_node` runs on the orchestrator (where the node IS in `ObjectManager`) and passes the resolved dict into `ExecuteNodeRequest.variables`. Workers receive a fully populated dict; they never need to fetch.

Critical: `aprocess_scope(request.variables)` — **not** `aprocess_scope(request.variables or None)`. An empty dict (`{}`) means "substitution enabled, no variables defined." `or None` converts that to `None`, which re-triggers the broken lazy fetch path.

## Dirty tracking

Nodes are marked UNRESOLVED (so they re-run) when:
1. A variable value changes at edit time → `_unresolve_nodes_referencing_variables()` walks all nodes
2. Before each flow run → `validate_before_workflow_run()` catches any nodes that became resolved between edits

Runtime re-queuing is not possible: the flow engine populates the execution queue before execution starts; marking a node UNRESOLVED mid-run has no effect on the current run. The pre-run hook is the reliable interception point.

## `ResolveSubstitutionRequest` behavior

- `names=[]` (default): returns all variables in scope — never errors, even if scope is empty
- `names=["FOO", "BAR"]`: per-name hierarchical lookup, all-or-nothing — fails if any name is not found (mirrors `SetVariablesRequest` semantics)

## Frontend variable listing

The frontend uses two distinct requests depending on context:

- **`ListVariablesRequest`** — returns `list[FlowVariable]` (user-defined variables only, typed objects). Used for the variable manager panel where users create/edit variables.
- **`ListSubstitutablesRequest`** — returns `list[Substitutable]` (user variables + project macros, unified). Use this for any picker/autocomplete that shows what can go in a `{VAR}` token. Each `Substitutable` has `name`, `value`, `source` (`"variable"` | `"macro"`), and `read_only`.

The `source` field exists so the frontend can render macros differently (e.g., a read-only badge). Future sources (files, env vars, etc.) add to this list without changing the response shape.

## Project macro variables

In addition to user-defined workflow variables, the substitution context also includes:

**Builtins** (always available when a project is loaded):
- `{workspace_dir}` — workspace directory path
- `{workflow_name}` — current workflow name
- `{workflow_dir}` — directory containing the saved workflow file (omitted if workflow not yet saved)
- `{static_files_dir}` — static files directory
- `{project_dir}` — project directory

**Project template directories** — any custom directories defined in the project config (e.g., `{inputs}`, `{outputs}`)

User-defined workflow variables take priority over all project-level variables.

**How it works**: `NodeExecutor._resolve_project_macro_variables()` calls `GetCurrentProjectRequest`, then `ProjectManager.get_project_substitution_variables(project_info)`. That method uses `_build_variable_resolver` to build a resolver and iterates over `BUILTIN_VARIABLES` and `template.directories`, silently skipping anything that can't be resolved. The result is merged under workflow variables in `_resolve_variables_for_node`:

```python
project_vars = NodeExecutor._resolve_project_macro_variables()
workflow_vars = VariableResolver._filter_for_substitution(var_result.variables)
return {**project_vars, **workflow_vars}  # workflow vars win
```
