# Load OCIO Config

**Loads an OpenColorIO config from the `$OCIO` environment variable or an explicit override path and emits it as an `OCIOConfigArtifact` for downstream nodes.**

Category: `Colorspace`

## TL;DR

- If `$OCIO` is set in your environment, the node detects it automatically — just add the node and run.
- `$OCIO` is re-read live at every execution, so Griptape project-level environment overrides (`project.yml` `environment:`) are always honored.
- Output is an `OCIOConfigArtifact` — wire it into [OCIO Color Parameters](ocio_color_parameters.md) or any node with an `OCIO Config` input.
- The collapsed **Advanced** group lets you override `$OCIO` with an explicit `.ocio` file path for testing.

## Typical workflow position

```text
[Load OCIO Config] → OCIO Color Parameters → (downstream transform/display nodes)
```

## Node preview

<!-- TODO: add ../assets/load-ocio-config.png screenshot -->

## Inputs

| Name           | Type   | Required | Notes                                                                                                  |
| -------------- | ------ | -------- | ------------------------------------------------------------------------------------------------------ |
| `context_vars` | `dict` | No       | OCIO context variables, e.g. `{"SHOT": "sh010", "SEQ": "sq020"}`. Carried on the output artifact.      |
| `file_path`    | `str`  | No       | Path to an `.ocio` config file. Only used when **Override OCIO Config** is enabled. Has a file picker. |

## Outputs

| Name             | Type                 | Notes                                                   |
| ---------------- | -------------------- | ------------------------------------------------------- |
| `config`         | `OCIOConfigArtifact` | Carries the resolved config path and context variables. |
| `was_successful` | `bool`               | Whether the config loaded successfully.                 |
| `result_details` | `str`                | Load result details (collapsed status group).           |

## Parameters

### Advanced *(collapsed by default)*

| Name                   | Type      | Default | Notes                                                                                                                                         |
| ---------------------- | --------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `override_ocio_config` | bool      | `False` | When enabled, uses the explicit `file_path` below instead of `$OCIO`. Useful for testing a specific config without changing your environment. |
| `file_path`            | file path | `""`    | Revealed when the override toggle is on.                                                                                                      |

## Tips & pitfalls

- **Watch the inline messages.** The node shows an info message with the detected `$OCIO` path, a warning when `$OCIO` is missing, and a warning when `$OCIO` changed since the last run (re-run the node to refresh downstream outputs).
- **Override mode replaces `$OCIO` entirely.** While **Override OCIO Config** is enabled, the environment variable is ignored; disable the toggle to return to environment-variable mode.
- **No `$OCIO` and no override means the node fails.** Set one or the other; per-project configs are cleanest via `project.yml` `environment:`.

## See also

- [OCIO Color Parameters](ocio_color_parameters.md) — the most common consumer of the `config` output.
