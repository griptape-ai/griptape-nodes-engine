# Macros

A macro is a template string that generates a file path by substituting named variables. Macros are used in situation templates and directory definitions.

Before diving into the full syntax, here are two examples that show what macros look like in practice:

```
Template:  {outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}

With all variables:
  outputs="outputs", node_name="ImageGen", file_name_base="render", _index=2, file_extension="png"
  → outputs/ImageGen_render002.png

With optional variables omitted:
  outputs="outputs", file_name_base="render", file_extension="png"
  → outputs/render.png
```

`{outputs}` is a directory name that the project system supplies automatically. `{node_name?:_}` is optional — when present, its value is followed by `_`; when absent, the block disappears entirely. `{_index?:03}` is optional and zero-padded to three digits when present.

## Variable syntax reference

### Required variable

```
{variable_name}
```

The variable must be provided. If it is missing when the macro is resolved, resolution fails with an error.

### Optional variable

```
{variable_name?}
```

The `?` marks the variable as optional. If the variable is not provided, the `{}` block — and any format spec — is omitted entirely from the output. The rest of the macro continues normally.

### Separator format

```
{variable_name:separator}
```

Appends `separator` after the variable's value. Any text that is not a recognized keyword (see transformations below) and not a numeric padding is treated as a separator.

This is most useful for building path prefixes that disappear cleanly when the variable is absent. For example, `{node_name?:_}` adds `node_name_` before a filename when the node name is known, but produces nothing at all when it isn't:

```
{node_name?:_}{file_name_base}

  node_name="ImageGen", file_name_base="render"  →  ImageGen_render
  node_name not provided,  file_name_base="render"  →  render
```

Path separators work the same way — `{sub_dirs?:/}` adds a subdirectory prefix only when sub-directories are specified:

```
{outputs}/{sub_dirs?:/}{file_name_base}.{file_extension}

  sub_dirs="lighting/pass_a", file_name_base="render", file_extension="exr"
  → outputs/lighting/pass_a/render.exr

  sub_dirs not provided, file_name_base="render", file_extension="exr"
  → outputs/render.exr
```

### Numeric padding

```
{variable_name:03}
```

Zero-pads the value to the specified width. The variable must hold an integer value.

```
{_index:03}   with _index = 5   → "005"
{_index:04}   with _index = 12  → "0012"
```

Used for auto-incrementing filenames under the `create_new` collision policy. Numeric padding (`:NN`) on a single unresolved variable is the opt-in: the first save lands at index `1` (or omitted, for the optional form), and subsequent saves walk forward against the same template — the padding format is preserved across the whole sequence.

- **Optional form** `{_index?:03}` — absent on the first save, then `_001`, `_002`, … on collision (padded width preserved).
- **Required form** `{_index:03}` — present from the first save: `_001`, `_002`, `_003`, … with consistent zero-padded width across the whole sequence.

```
Template: {file_name_base}_v{_index:03}.{file_extension}

  Save #1 → render_v001.png
  Save #2 → render_v002.png
  Save #3 → render_v003.png
```

The variable name does not need to be `_index`; any single unresolved required variable with `:NN` padding will be auto-allocated. Without padding, an unresolved required variable is treated as a missing binding (a configuration error) and the save fails — this prevents `{shot}` from silently being filled with `1, 2, 3, …` when the user forgot to wire it up.

### Sequence slot (`###`)

```
###       → 1-digit minimum (1, 2, ..., 9, 10, 11, ...)
###       → 3-digit minimum (001, 002, ..., 999, 1000, ...)
####      → 4-digit minimum (0001, 0002, ..., 9999, 10000, ...)
```

A run of `#` characters in static text is the explicit syntax for a sequence slot. Each `#` contributes one digit to the **minimum** render width. Values below `10 ^ width` are zero-padded to that width; values at or above it render at their natural width (no truncation). This matches the universal `###` convention from ffmpeg (`%03d`), Houdini (`$F4`), Nuke (`####`), and Python's `:03` format spec.

```
Template: {file_name_base}_v###.{file_extension}

  Save #1    → render_v001.png
  Save #2    → render_v002.png
  ...
  Save #999  → render_v999.png
  Save #1000 → render_v1000.png   (overflow: 4 digits, not truncated)
```

Use `###` whenever you want a system-allocated sequence index. It says "this slot is what `create_new` should advance on collision" without leaning on the numeric-padding heuristic described above, so a macro author who genuinely needs a user-bound `{shot:03}` variable can write that without ambiguity.

**One sequence slot per macro.** A template with two `#` runs (e.g. `v###_take_##.png`) is rejected at parse time — the system has no way to know which slot to auto-allocate. Compose the second number as an explicit `{var}` if you need it.

**Relationship to `{_index:NN}`.** Internally `###` desugars to a variable named `_index` carrying a sequence-format marker. The legacy `{_index:03}` / `{_index?:03}` syntax continues to work and is still treated as a sequence slot for backward compatibility, but `###` is the recommended form going forward. Future versions may retire the `{_index:NN}` shorthand once project templates have migrated; see [issue #4902](https://github.com/griptape-ai/griptape-nodes-engine/issues/4902).

### String transformations

```
{variable_name:lower}    → lowercase
{variable_name:upper}    → UPPERCASE
{variable_name:slug}     → slug-form (spaces to hyphens, safe chars only)
```

For example, if `workflow_name` is `"My Autumn Shoot"`:

```
{workflow_name:lower}  →  "my autumn shoot"
{workflow_name:slug}   →  "my-autumn-shoot"
```

### Default value

```
{variable_name|default_value}
```

If the variable is not provided, `default_value` is used instead.

```
{workflow_name|untitled}   → uses "untitled" if workflow_name is not provided
```

### Chaining format specs

Multiple format specs are separated by `:` and applied left to right. If a separator is used, it must come first:

```
{variable_name:_:lower}    → lowercase value with underscore appended
{variable_name:lower:slug} → lowercase, then slug
```

### Quoted separators

If your separator text matches a keyword like `lower` or `upper`, wrap it in single quotes to treat it as a literal separator:

```
{variable_name:'lower'}    → appends the text "lower" as a separator
```

## Resolution

When a macro is resolved, directory names and builtin variables are supplied automatically by the project system. You only need to provide the variables specific to your operation (like `file_name_base` and `file_extension`).

For example, resolving the `save_node_output` situation macro:

```
Template:   {outputs}/{sub_dirs?:/}{node_name?:_}{file_name_base}{_index?:03}.{file_extension}

Automatic:  outputs → resolved from the "outputs" directory definition → "outputs"
Provided:   node_name="StyleTransfer", file_name_base="portrait", _index=3, file_extension="png"
Result:     outputs/StyleTransfer_portrait003.png
```

Directory names (like `outputs`) are automatically resolved to their configured paths. See [Directories](directories.md).

Builtin variables (like `workflow_name`, `project_dir`) are also supplied automatically. See [Environment & Builtin Variables](environment.md).

## Reverse matching

The macro system can also work in reverse: given an actual path and a macro template, it can extract the values of the variables. This is used when the system needs to identify whether a file belongs to a known project directory and what metadata is encoded in its name.

For example:

```
Template:  {outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}
Path:      outputs/StyleTransfer_portrait003.png
Extracted: outputs="outputs", node_name="StyleTransfer", file_name_base="portrait", _index=3, file_extension="png"
```

Numeric padding is reversed by parsing the number (`"003"` → integer `3`). Case transformations and slugification cannot be reliably reversed and return the value as-is.

## Syntax errors

The macro parser reports syntax errors with a position number to help you find the problem:

- Unclosed brace: `{variable_name` (no closing `}`)
- Unmatched closing brace: `variable}name`
- Nested braces: `{outer{inner}}`
- Empty variable: `{}`
