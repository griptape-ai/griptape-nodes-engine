# Macros

A macro is a template string that generates a file path by substituting named variables. Macros are used in situation templates and directory definitions.

Before diving into the full syntax, here are two examples that show what macros look like in practice:

```
Template:  {outputs}/{node_name?:_}{file_name_base}{###?}.{file_extension}

First save (no collision):
  outputs="outputs", node_name="ImageGen", file_name_base="render", file_extension="png"
  → outputs/ImageGen_render.png

Second save (collides with first, sequence slot fills in):
  same variables as above
  → outputs/ImageGen_render001.png

With optional variables omitted:
  outputs="outputs", file_name_base="render", file_extension="png"
  → outputs/render.png
```

`{outputs}` is a directory name that the project system supplies automatically. `{node_name?:_}` is optional — when present, its value is followed by `_`; when absent, the block disappears entirely. `{###?}` is an optional [sequence slot](#sequence-slot-) — omitted on the first save, then `001`, `002`, … on collision.

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
{shot:03}   with shot = 5   → "005"
{shot:04}   with shot = 12  → "0012"
```

Numeric padding is a **string-formatting** concern: it controls how an integer value renders. It does NOT mark the slot as system-allocated. If you want a slot that the engine fills with the next sequence number under `create_new` policy, see [Sequence slot (`{###}`)](#sequence-slot-) below.

> **Compatibility note:** Earlier engine versions treated `{_index:NN}` (or any single unresolved padded variable like `{shot:03}`) as an implicit opt-in to auto-allocation. That heuristic was retired in favor of the explicit `{###}` syntax. A custom project template that still uses `{_index:NN}` to opt into auto-indexing will now surface `MISSING_REQUIRED_VARIABLES` on save — rewrite the slot as `{###}` (or `{###?}` for the optional form). See [issue #4991](https://github.com/griptape-ai/griptape-nodes-engine/issues/4991).

### Sequence slot (`{###}`)

```
{#}       → 1-digit minimum (1, 2, ..., 9, 10, 11, ...)
{###}     → 3-digit minimum (001, 002, ..., 999, 1000, ...)
{####}    → 4-digit minimum (0001, 0002, ..., 9999, 10000, ...)
{##?}     → 2-digit minimum, optional (omitted on first save; 01, 02, … on collision)
```

A run of `#` characters **inside `{}` braces** is the explicit syntax for a sequence slot. Each `#` contributes one digit to the **minimum** render width. Values below `10 ^ width` are zero-padded to that width; values at or above it render at their natural width (no truncation). This matches the universal `###` convention from ffmpeg (`%03d`), Houdini (`$F4`), Nuke (`####`), and Python's `:03` format spec.

A trailing `?` inside the braces (e.g. `{##?}`) marks the slot **optional** — the same rule as for any other variable. Optional slots are omitted on the first save and only fill in on collision.

```
Template: {file_name_base}_v{###}.{file_extension}

  Save #1    → render_v001.png
  Save #2    → render_v002.png
  ...
  Save #999  → render_v999.png
  Save #1000 → render_v1000.png   (overflow: 4 digits, not truncated)
```

```
Template (optional): {file_name_base}{##?}.{file_extension}

  Save #1 → render.png            (slot omitted)
  Save #2 → render01.png          (slot fills in on collision)
  Save #3 → render02.png
```

Use `{###}` whenever you want a system-allocated sequence index. It says "this slot is what `create_new` should advance on collision" without leaning on the numeric-padding heuristic described above, so a macro author who genuinely needs a user-bound `{shot:03}` variable can write that without ambiguity.

**Why the `{}` wrapping.** Macro templates often appear in places where bare `#` chars have other meanings — Markdown headers, comments, shell scripts. Wrapping the sigil inside `{}` keeps the sequence-slot syntax inside the same delimiters that already mark "this is a macro variable," so authors don't need escaping rules for stray `#` chars in static text.

**One sequence slot per macro.** A template with two `{###}` blocks (e.g. `{###}_take_{##}.png`) is rejected at parse time — the system has no way to know which slot to auto-allocate. Compose the second number as an explicit `{var}` if you need it.

**Relationship to `{_index:NN}`.** Internally `{###}` desugars to a variable named `_index` carrying a sequence-format marker. The legacy `{_index:03}` / `{_index?:03}` syntax still parses, but it's now treated as a regular user-bound padded variable — the auto-allocation behavior is reserved for the explicit `{###}` form. If you have a custom project template that uses `{_index:NN}` for sequence slots, rewrite it as `{###}` (or `{###?}` for the optional form). See [issue #4991](https://github.com/griptape-ai/griptape-nodes-engine/issues/4991) for the migration context.

### String transformations

| Format spec        | Description                                      | Example result      |
| ------------------ | ------------------------------------------------ | ------------------- |
| `:lower`           | All lowercase                                    | `"my autumn shoot"` |
| `:upper`           | All uppercase                                    | `"MY AUTUMN SHOOT"` |
| `:title`           | Title Case                                       | `"My Autumn Shoot"` |
| `:snake`           | snake_case                                       | `"my_autumn_shoot"` |
| `:pascal`          | PascalCase                                       | `"MyAutumnShoot"`   |
| `:camel`           | camelCase                                        | `"myAutumnShoot"`   |
| `:screaming_snake` | SCREAMING_SNAKE_CASE                             | `"MY_AUTUMN_SHOOT"` |
| `:slug`            | Slug (spaces→hyphens, non-alphanumeric stripped) | `"my-autumn-shoot"` |
| `:dot`             | dot.case                                         | `"my.autumn.shoot"` |
| `:abbrev`          | First letter of each word                        | `"MAS"`             |
| `:trim`            | Strip leading/trailing whitespace                | `"My Autumn Shoot"` |

For example, if `workflow_name` is `"My Autumn Shoot"`:

```
{workflow_name:lower}           →  "my autumn shoot"
{workflow_name:upper}           →  "MY AUTUMN SHOOT"
{workflow_name:title}           →  "My Autumn Shoot"
{workflow_name:snake}           →  "my_autumn_shoot"
{workflow_name:pascal}          →  "MyAutumnShoot"
{workflow_name:camel}           →  "myAutumnShoot"
{workflow_name:screaming_snake} →  "MY_AUTUMN_SHOOT"
{workflow_name:slug}            →  "my-autumn-shoot"
{workflow_name:dot}             →  "my.autumn.shoot"
{workflow_name:abbrev}          →  "MAS"
```

`:snake`, `:pascal`, `:camel`, `:dot`, and `:screaming_snake` also handle camelCase and PascalCase input correctly by splitting on case transitions, so `{varName:snake}` → `"var_name"` works as expected.

`:trim` is most useful as a pre-processing step before another transformation, e.g. `{name:trim:snake}` strips surrounding whitespace and then converts to snake_case.

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
Template:   {outputs}/{sub_dirs?:/}{node_name?:_}{file_name_base}{###?}.{file_extension}

Automatic:  outputs → resolved from the "outputs" directory definition → "outputs"
Provided:   node_name="StyleTransfer", file_name_base="portrait", file_extension="png"
Result (first save):       outputs/StyleTransfer_portrait.png
Result (second, on collision): outputs/StyleTransfer_portrait001.png
```

Directory names (like `outputs`) are automatically resolved to their configured paths. See [Directories](directories.md).

Builtin variables (like `workflow_name`, `project_dir`) are also supplied automatically. See [Environment & Builtin Variables](environment.md).

## Reverse matching

The macro system can also work in reverse: given an actual path and a macro template, it can extract the values of the variables. This is used when the system needs to identify whether a file belongs to a known project directory and what metadata is encoded in its name.

For example:

```
Template:  {outputs}/{node_name?:_}{file_name_base}{###?}.{file_extension}
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
