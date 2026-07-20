# Nuke Start Flow

**Entry point for a Nuke-targeted workflow — required for publishing the workflow as a Nuke gizmo.**

Category: `Foundry Nuke`

## TL;DR

- Place this at the start of the top-level flow of any workflow you intend to publish as a `.gizmo`.
- Behaves like the standard [Start Flow](../../nodes/execution/start_flow.md) node; the Nuke variant marks the workflow as Nuke-targeted for the gizmo publisher.
- Pair with [Nuke End Flow](nuke_end_flow.md) at the other end of the flow.

## Typical workflow position

```text
[Nuke Start Flow] → (workflow nodes) → Nuke End Flow
```

## Inputs

None.

## Outputs

| Name       | Type    | Notes                                             |
| ---------- | ------- | ------------------------------------------------- |
| `exec_out` | control | Control connection to the first node in the flow. |

## Tips & pitfalls

- **Required for gizmo publishing.** The publisher validates that the top-level flow starts with a Nuke Start Flow and ends with a [Nuke End Flow](nuke_end_flow.md); a plain Start Flow will not validate.

## See also

- [Nuke End Flow](nuke_end_flow.md) — the matching terminal node.
- [Nuke Script](nuke_script.md) — run a `.nk` script inside the flow.
