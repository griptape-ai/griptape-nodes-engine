# Nuke End Flow

**Terminal node for a Nuke-targeted workflow; reports whether the flow succeeded.**

Category: `Foundry Nuke`

## TL;DR

- Place this at the end of the top-level flow of any workflow you intend to publish as a `.gizmo`.
- Exposes `was_successful` and `result_details` so the published gizmo can report run status back inside Nuke.
- Pair with [Nuke Start Flow](nuke_start_flow.md) at the other end of the flow.

## Typical workflow position

```text
Nuke Start Flow → (workflow nodes) → [Nuke End Flow]
```

## Inputs

| Name      | Type    | Required | Notes                                              |
| --------- | ------- | -------- | -------------------------------------------------- |
| `exec_in` | control | Yes      | Control connection from the last node in the flow. |

## Outputs

| Name             | Type   | Notes                                    |
| ---------------- | ------ | ---------------------------------------- |
| `was_successful` | `bool` | Whether the flow completed successfully. |
| `result_details` | `str`  | Details about the flow result.           |

## Tips & pitfalls

- **Required for gizmo publishing.** The publisher validates that the top-level flow starts with a [Nuke Start Flow](nuke_start_flow.md) and ends with a Nuke End Flow; a plain End Flow will not validate.

## See also

- [Nuke Start Flow](nuke_start_flow.md) — the matching entry node.
- [Nuke Script](nuke_script.md) — run a `.nk` script inside the flow.
