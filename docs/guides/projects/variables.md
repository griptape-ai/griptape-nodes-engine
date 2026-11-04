# Project Variables

The `variables` section of a project file declares named values that belong to the project. They join the same `{VAR}` substitution system as workflow variables and can be used in node parameter values and in macros.

```yaml
variables:
  shot_code:
    value: sc042
  frame_start:
    value: 1001
    type: int
  facility:
    value: mtl
    permission: read_only
```

## Fields

| Field        | Required | Default      | Meaning                                                                                                                                                                                                                               |
| ------------ | -------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `value`      | yes      | —            | The variable's value. Must be a string or an integer, matching the declared `type`.                                                                                                                                                   |
| `type`       | no       | `str`        | `str` or `int`. The value must agree with it — declaring `type: int` with a quoted string is a load error.                                                                                                                            |
| `permission` | no       | `read_write` | `read_write` variables can be changed at runtime (and the change is saved back to this file). `read_only` variables can only be changed by editing the project file. `write_only` is accepted but not yet fully enforced — see below. |

Booleans and floats are not supported as variable values. Only string and integer values can substitute into a `{VAR}` token.

!!! warning "`write_only` is not yet read-restricted"

    A `write_only` variable accepts runtime writes like `read_write`, but the engine does
    not yet hide its value on reads — today it behaves like `read_write` everywhere a value
    is displayed or substituted. **Do not use it to hold secrets yet.** Read restriction is
    planned as part of upcoming secrets support; when that lands, `write_only` values will
    stop appearing in reads, pickers, and `{VAR}` substitution.

## How project variables resolve

Variables are looked up in layers, with closer layers winning on a name conflict:

1. **Workflow (flow) variables** — variables created in the workflow editor
1. **Project variables** — this section, plus the project's builtins and directories
1. **Global variables**

Within the project layer, **builtins and directory names always win** over a `variables:` entry of the same name. Declaring a variable named `workspace_dir` or the name of one of your directories produces a load warning, and that variable can never resolve — pick a different name.

Builtin and directory names are also **reserved**: the engine refuses to create or rename any workflow or global variable to one of those names.

## Runtime writes and persistence

A `read_write` project variable can be changed while the engine runs — from the variable panel or by a node that sets variables. Each successful change is immediately saved back to the project file, so it survives restarts.

Writes are checked against the declared type: setting a string value on an `int` variable is refused with an error rather than silently coerced.

`read_only` variables refuse all runtime writes. To change one, edit the project file and reload the project.

## Project variables in macros

Project variables participate in path macro resolution (for example in a directory `path_macro` or a situation macro), below caller-supplied values and above the project `environment:` section:

1. Builtin variables
1. Directory names
1. Caller-supplied variables
1. **Project variables** (this section)
1. Project environment variables
1. Shell environment variables

See [Environment & Builtin Variables](environment.md#variable-priority) for the full priority discussion.

## Inheritance

When a project declares a parent (`parent_project_path` / `parent_project_id`), variables merge per-entry:

- A child entry with the same name **replaces** the parent's entry entirely.
- A child entry set to `null` **removes** the inherited variable:

```yaml
# child project.yml
variables:
  shot_code:
    value: sc099      # overrides the parent's shot_code
  facility: null       # removes the inherited facility variable
```

- Parent entries the child doesn't mention are inherited unchanged.

Deleting an inherited variable at runtime writes the `null` tombstone into the child's file, so the removal also survives reloads.

## Choosing between `variables` and `environment`

Both are project-scoped key-value sections; they serve different consumers:

- **`variables`** entries are first-class engine variables: they appear in the variable panel and pickers, carry a type and a permission, can be changed at runtime, and participate in `{VAR}` substitution inside node parameters.
- **`environment`** entries exist for macro composition (paths, codes) and OS environment export. They are strings only, are not runtime-writable, and don't appear as variables in the editor.

Rule of thumb: if an artist should see or change it, make it a variable; if it's plumbing for path macros, keep it in `environment`.
