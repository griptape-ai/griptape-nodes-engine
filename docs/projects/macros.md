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

**Trailing form.** The `?` can also appear at the end of the last format spec — `{shot:upper?}` is equivalent to `{shot?:upper}`. Both spellings mark the variable optional. Applies to plain variables and sequence shorthand alike: `{###:upper?}` is the same as `{###?:upper}`. Quote the separator (`{shot:'lower?'}`) to keep the `?` as a literal character.

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

### Leading separator

```
{variable_name:^prefix}
```

Mirror of the [separator format](#separator-format), but the text is **prepended** to the variable's value rather than appended. Marked by a `^` at the start of the format-spec text; anything after the `^` is the literal prefix payload. Renders iff the variable emits — an unbound optional variable takes its leading separator with it into oblivion.

```
{file_name_base}{version?:^_v}.{file_extension}

  file_name_base="render", version=3, file_extension="png"  →  render_v3.png
  file_name_base="render", version not provided             →  render.png
```

The load-bearing pattern: pair with a [sequence slot](#sequence-slot-) to get a version suffix that comes and goes with the sequence:

```
render{###?:^_v}.png

  first save (slot omitted)  →  render.png
  second save (slot fires)   →  render_v001.png
  third save                 →  render_v002.png
```

Works outside filenames too — the trailing separator is not just for path prefixes and neither is the leading one:

```
Hello, {name?}!{intro?:^ Nice to meet you.}

  name="Alice", intro="y"  →  Hello, Alice! Nice to meet you.y
  name="Alice", intro absent →  Hello, Alice!
  name absent, intro absent  →  Hello, !
```

**Composition rules**

- A variable can carry at most **one** leading separator.
- The leading separator is applied **after** every other format spec on the same variable, regardless of where you write it in the template. `{shot:03:^_v}` and `{shot:^_v:03}` both render `shot=5` as `_v005` — the parser normalizes the leading spec to the tail of the list so ordering never mangles the prefix.

**Related grammar errors**

| Error                         | Cause                                      |
| ----------------------------- | ------------------------------------------ |
| `EMPTY_LEADING_SEPARATOR`     | `:^` with no payload after the caret       |
| `MULTIPLE_LEADING_SEPARATORS` | Two `:^`-marked specs on the same variable |

**Limitation.** A `^` at the start of a format spec is now reserved as the leading-separator discriminator, so a literal `^_v` prefix isn't spellable today. If a real use case surfaces, a future escape mechanism (e.g. `\^` or a `'^'`-quoted form) will fill it in.

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

**Relationship to `{_index:NN}`.** Internally `{###}` desugars to a variable named `_index` carrying a sequence-format marker. The legacy `{_index:03}` / `{_index?:03}` syntax continues to work and is still treated as a sequence slot for backward compatibility, but `{###}` is the recommended form going forward. Future versions may retire the `{_index:NN}` shorthand once project templates have migrated; see [issue #4902](https://github.com/griptape-ai/griptape-nodes-engine/issues/4902).

### Unresolved sequence slots

A required `{###}` slot has no value until the write path allocates one. Any code that resolves a macro *before* that allocation happens — a node previewing where its output will land, a UI classifying user input as absolute-vs-relative — has to tell the resolver what to do about the empty slot. `GetPathForMacroRequest` exposes the choice as `unresolved_sequence_slot_behavior`, whose values live in the `UnresolvedSequenceSlotBehavior` enum:

| Behavior                  | Renders as                               | When to use                                                                                                                                                                                                                                                                    |
| ------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `FAIL` *(default)*        | `MISSING_REQUIRED_VARIABLES` failure     | The **write path** — the failure is the signal `on_write_file_request` uses to seed the first index and retry on collision. Nothing else should override the default.                                                                                                          |
| `RENDER_SEQUENCE_PATTERN` | `###` (or `####`, matching source width) | **Presentation only.** Renders the slot as its bare hash glyphs (the universal ffmpeg / Houdini / Nuke convention) so the resulting path reads as its on-disk shape. Never open, write, or hand this string to any I/O primitive — the pattern is not a valid filesystem path. |
| `START_AT_ZERO`           | `000`                                    | Previewing 0-indexed sequences before the first save.                                                                                                                                                                                                                          |
| `START_AT_ONE`            | `001`                                    | Previewing "what would my first save land at" — matches the write-path seed, so the preview lines up with the real save when the destination is empty.                                                                                                                         |

Optional slots (`{###?}`) are unaffected — they're already omitted when unbound, so the flag only takes effect on required slots.

**Rule of thumb.** If your code is about to open a file, do not pass a flag; let the write path do its thing. If your code is about to show a string to a user, use `RENDER_SEQUENCE_PATTERN`. `START_AT_ZERO` / `START_AT_ONE` are narrow tools for previewing an actual first save.

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
