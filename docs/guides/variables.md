# Workflow Variables

A **variable** is a named value that lives on a flow instead of on any one
node. Create it once, then read or write it from anywhere that value needs
to show up — including inline inside text fields, using `{variable_name}`.

!!! info "Not the same as macro path variables"

    [Macros](projects/macros.md) are the template syntax the project system
    uses to build **file paths** (`{outputs}/{file_name_base}.{file_extension}`).
    Workflow variables are user-created values you store and reuse across a
    workflow's node parameters. The two systems share the same `{name}` /
    `{name:spec}` syntax and format specs under the hood, and project macros
    (like `workspace_dir` and `workflow_name`) even show up alongside your
    own variables when you type `{` in a text field — but a macro path
    template describes how to build a filename, while a workflow variable is
    a value you define and change during a run. If you're building a save
    path, read [Macros](projects/macros.md) instead.

## When to use a variable

Say three prompt fields across your workflow all need the same client name.
Wiring
a connection from one source node to every field that needs it works, but
it clutters the canvas and breaks if you ever want to type the value
directly into a field instead of dragging a wire.

A variable solves this without any connections: set it once, then
reference `{project_name}` inside as many text parameters as you like.
Change the variable's value and every field that references it picks up the
new value the next time it runs.

## Creating a variable

The **Variables** category in the node library has a small family of nodes
for creating and managing variables:

<!-- screenshot: the Variables category in the node library panel, showing Create Variable, Set Variable, Get Variable, Has Variable, Set Variables from Data, and Set Variable Substitution -->

- **Create Variable** — creates a variable with a name, type, and initial
    value, or updates it if a variable with that name already exists in the
    current flow. Connect any output into its `value` input and the
    variable's type is inferred automatically from that connection.
- **Set Variable** — sets a variable's value, creating the variable first
    if it doesn't exist yet. Its `variable_name` field is a dropdown of
    variables already in scope, plus a **Create new variable** option that
    reveals a name field when picked.
- **Set Variables from Data** — turns a dict, a JSON/YAML string, or a list
    of key-value pairs into several variables in one step. Useful when you
    already have a JSON blob (parsed config, an API response) and want each
    key to become its own variable instead of wiring up one **Set Variable**
    node per key.
- **Get Variable** — reads a variable's current value out as a normal
    output, for wiring into a node that doesn't support inline `{name}`
    substitution.
- **Has Variable** — checks whether a variable exists, so you can branch on
    it (for example, only create a variable the first time a flow runs).

<!-- screenshot: a Create Variable node configured with variable_name "project_name" and a text value wired into its value input -->

**Create Variable** and **Set Variable** always create the variable in the
node's own flow (flow-scoped, described below) — there's currently no node
that creates a global variable directly. The **scope** setting on these
nodes (under **Advanced**) controls where they *look* for an existing
variable, not where a new one is created.

These nodes treat the variable they touch as live state rather than a
resolved-once value. **Create Variable** forces itself back to unresolved
every time the workflow or the node runs, so it always re-applies its
current `value`. **Get Variable**, **Set Variable**, and **Has Variable**
check the variable's actual current value each time they're asked for their
resolution state, and mark themselves unresolved if it no longer matches
what they last read or wrote — so they pick up a change made elsewhere
(another **Set Variable** node, a different flow) without needing a manual
run.

## Scopes

Every variable request — create, get, set, list — takes a **scope**, which
controls where the lookup searches:

| Scope                    | Behavior                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `hierarchical` (default) | Search the current flow, then each ancestor flow up to the root, then global variables. The first match wins.                 |
| `current_flow_only`      | Only the exact flow the node is in. Ignores parent flows and globals entirely.                                                |
| `global_only`            | Only global variables — variables not owned by any flow.                                                                      |
| `all`                    | Every variable from every flow, primarily for enumeration (populating a picker or list) rather than resolving a single value. |

`hierarchical` is what you want almost always: define `project_name` once
in the top-level flow, and every sub-flow nested inside it can read
`{project_name}` without redeclaring it. If a nested flow defines its own
`project_name`, that nested definition **shadows** the parent's for
everything inside it — the parent's value is untouched and reappears once
you leave the nested flow.

A shorter way to think about it: the definition closest to where you're
reading wins, and flow-scoped variables win over global ones.

## Variable types

A variable's `type` is the same type label you'd see on any node
parameter — `str`, `int`, `float`, `bool`, `json`, an image type, and so on.
**Create Variable** and **Set Variable** infer the type automatically from
whatever you connect to their `value` input; you don't need to set it by
hand unless you're leaving `value` unconnected and typing a literal
directly into the node.

Only variables holding a `str` or `int` value (not `bool`) can be
substituted inline into a `{name}` token in a text field — see
[Inline substitution](#inline-substitution-in-text-fields) below. Variables
holding other types (JSON, images, lists) still work fine with **Get
Variable** / **Set Variable**; they just can't be dropped into a text field
as a bare token, since there's no sensible way to render an image inline as
text.

## Setting and reading values during a run

Outside of nodes, if you're scripting against the engine's request API
directly, the relevant requests are `CreateVariableRequest`,
`GetVariableValueRequest`, and `SetVariableValueRequest` — each takes a
`name` and a `lookup_scope`. `SetVariableValueRequest` unresolves any
already-resolved node downstream that references the variable, so changing
a variable mid-editing correctly invalidates anything that was computed
from its old value.

For inline substitution, one more layer sits on top of the plain variable
lookup: when a node runs, the engine resolves every `{name}` token in its
parameter values against a merged set of **your workflow variables plus the
project's read-only macro values** (`workspace_dir`, `workflow_name`, and
any project template directories) — your own variables take priority if a
name collides with one of those. That merge is what lets a text field
reference `{workflow_name}` and `{project_name}` side by side even though
one comes from the project and the other from a variable you created.

## Inline substitution in text fields

Type `{` inside any text parameter that supports it and the editor pops up
a variable picker listing every variable and project macro currently in
scope, grouped by source:

<!-- screenshot: a text parameter field with the { picker open, showing grouped variables and project macros -->

Pick one, or just type the name and close the brace yourself —
`{project_name}` — and its value is substituted in at execution time. If
the name doesn't resolve to anything in scope, the token is left in the
text untouched (or, for an optional token, silently dropped — see below),
so a typo doesn't silently produce a wrong value.

You can turn this behavior off per workflow with the **Set Variable
Substitution** node if you specifically want `{literal text like this}` to
pass through untouched instead of being treated as a token.

### Format specs

A token can carry format specs after a `:`, applied left to right, using
the same syntax and the same underlying spec list documented in full in
[Macros → String transformations](projects/macros.md#string-transformations):

| Spec               | Result                            |
| ------------------ | --------------------------------- |
| `:lower`           | all lowercase                     |
| `:upper`           | ALL UPPERCASE                     |
| `:title`           | Title Case                        |
| `:snake`           | snake_case                        |
| `:pascal`          | PascalCase                        |
| `:camel`           | camelCase                         |
| `:screaming_snake` | SCREAMING_SNAKE_CASE              |
| `:slug`            | url-safe-slug                     |
| `:dot`             | dot.case                          |
| `:abbrev`          | first letter of each word         |
| `:trim`            | strip leading/trailing whitespace |

```
{project_name}            → "Autumn Campaign"
{project_name:lower}      → "autumn campaign"
{project_name:slug}       → "autumn-campaign"
{project_name:snake:upper} → "AUTUMN_CAMPAIGN"
```

Two more pieces of syntax carry over from macros and work the same way here:

- **Optional marker `?`** — `{project_name?}` renders as an empty string
    instead of leaving the literal token behind when the variable isn't
    found, so you can build sentences that gracefully drop a clause when a
    variable is unset.
- **Default value `|default`** — `{project_name|untitled}` substitutes
    `untitled` if `project_name` isn't in scope.

Numeric padding (`:03`), sequence slots (`{###}`), and leading separators
(`:^prefix`) are also parsed by the same engine and technically valid inside
a text field, but they exist for building filenames with auto-incrementing
counters — see [Macros](projects/macros.md) if you need that behavior. For
ordinary text substitution, the transformations and optional/default specs
above cover the common cases.

## Worked example: a project name across several prompts

Say a workflow generates concept art and needs the same project name in
several prompt fields.

1. Drop a **Create Variable** node near the top of the flow. Set
    `variable_name` to `project_name`, type `Autumn Campaign` directly into
    `value` (no connection needed for a literal), and let `variable_type`
    stay as the inferred `str`.

1. In your prompt-building node's text field, type:

    ```
    A cinematic key art poster for {project_name}, dramatic lighting, wide shot
    ```

1. In a second prompt field elsewhere in the flow:

    ```
    Concept sketch, {project_name:slug} style guide, muted palette
    ```

1. Run the flow once. Both prompts pick up "Autumn Campaign" (or whatever
    you typed), with the second one rendering `autumn-campaign` where the
    slug spec applies.

<!-- screenshot: the finished flow with a Create Variable node feeding project_name into two prompt fields that show the resolved {project_name} text -->

To change the project for a new run, edit the value on the **Create
Variable** node (or wire a different source into it) — every field
referencing `{project_name}` updates the next time the flow executes,
without rewiring anything.
