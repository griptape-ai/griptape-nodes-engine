# Cedar Policies

Permission templates are written in [Cedar](https://www.cedarpolicy.com/), Amazon's open-source authorization language. The [Permission Editor](admin_dashboard.md#permission-editor)'s **Permission Builder** compiles your choices into Cedar for you. This page is the reference for the other path: **Raw Cedar** templates you write by hand, and auditing the Cedar a builder template produced.

Reach for Raw Cedar when the builder's capability catalog does not cover a rule you want. If the builder can express the rule, let it: builder output always parses.

## What a policy decides on

Cedar evaluates every decision as a `(principal, action, resource, context)` tuple. Griptape Nodes asks for a decision at **checkpoints**: the moments a privileged operation is about to happen. By then the engine has resolved the real thing being acted on, so a rule reads typed attributes on it rather than raw request data.

| Part        | What it holds                                                        | Use it for                                           |
| ----------- | -------------------------------------------------------------------- | ---------------------------------------------------- |
| `principal` | A single anonymous user, `User::"<anonymous>"`.                      | Nothing. Leave it unconstrained.                     |
| `action`    | The checkpoint, e.g. `Action::"LoadLibrary"`.                        | Naming which operation a rule covers.                |
| `resource`  | The resolved library, node type, project, model, or codec.           | Matching a specific thing, or a fact about it.       |
| `context`   | Ambient facts: the active project, the engine, the license identity. | Scoping a rule to one project, or to a license type. |

A rule keyed to a specific principal matches nothing, because there is one anonymous principal for everybody. Per-user rules do not exist yet; give a user their own license key and attach different templates to it instead.

## How decisions combine

Two rules govern every template:

1. An operation is allowed only when some `permit` matches it.
1. A matching `forbid` beats every `permit`, from any template.

That maps onto the builder's two postures:

- **Exploration (Allow all)** opens with a catch-all `permit(principal, action, resource);` and carves exceptions out of it with `forbid` statements.
- **Production (Deny All)** emits no catch-all. Every checkpoint falls to Cedar's default deny unless a `permit` names it.

A Production template is a *soft* deny: it emits no `forbid`, so another template on the same license key can still permit what it leaves out. A `forbid` is a hard block, and nothing overrides one. That includes a `forbid` in a Griptape-managed platform policy, which you cannot edit or out-vote with a template of your own.

!!! warning "A `forbid` on its own does not make a blocklist"

    Cedar has no implicit allow. If nothing attached to a license key carries a `permit`, then everything the `forbid` statements do not mention is denied too, because no `permit` ever matched.

    Every template attached to a key is evaluated as one combined set, so the catch-all can live in a different template than the `forbid`. The Exploration posture is what supplies it:

    ```cedar
    permit(principal, action, resource);
    ```

## Checkpoints

Each action below is a point the engine gates. A denial surfaces differently at each one.

| Action                      | Fires when                                                | Resource     | A denial looks like                                                                                           |
| --------------------------- | --------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------- |
| `Action::"LoadLibrary"`     | A library loads past its metadata stage.                  | `Library`    | The library is marked unusable, with the reason on its error icon.                                            |
| `Action::"InstantiateNode"` | A node is created.                                        | `NodeType`   | An Error Proxy node in place of the real one. Denied node types are also listed on the library ahead of time. |
| `Action::"LoadProject"`     | A project template is read.                               | `Project`    | The project fails to load.                                                                                    |
| `Action::"ActivateProject"` | A project becomes the current one.                        | `Project`    | The switch fails and the current project stays put.                                                           |
| `Action::"OfferModel"`      | A model picker is built.                                  | `Model`      | The model is filtered out of the picker.                                                                      |
| `Action::"InvokeModel"`     | A node calls a model.                                     | `Model`      | The invocation fails.                                                                                         |
| `Action::"ReadVideoCodec"`  | Video is about to be read, and when a picker is built.    | `VideoCodec` | The read is refused, or the codec is filtered out.                                                            |
| `Action::"WriteVideoCodec"` | Video is about to be written, and when a picker is built. | `VideoCodec` | The write is refused, or the codec is filtered out.                                                           |

`OfferModel` and `InvokeModel` are separate on purpose. Gating both keeps a picker honest: a model you deny disappears from dropdowns *and* cannot be called by a node that was already bound to it.

!!! note "Reserved action names"

    `Action::"LoadNodeType"` and `Action::"ListModel"` are part of the policy model but the engine never asks about them. A rule naming either one matches nothing.

## Resource attributes

Every attribute below is optional except where noted. Guard an optional attribute with `has` before reading it (see [the gotchas](#guard-every-optional-attribute)).

### `Library`

| Attribute         | Type   | Present when                  |
| ----------------- | ------ | ----------------------------- |
| `id`              | string | Always. The library name.     |
| `lifecycle_stage` | string | The library declares a stage. |

### `NodeType`

| Attribute                 | Type        | Present when                                                                   |
| ------------------------- | ----------- | ------------------------------------------------------------------------------ |
| `id`                      | string      | Always. The node type name.                                                    |
| `executes_arbitrary_code` | bool        | Always. `true` when the node runs Python supplied at runtime.                  |
| `lifecycle_stage`         | string      | The node declares a stage, or inherits one from its library.                   |
| `model_ids`               | set<string> | The node declares model usage.                                                 |
| `provider_ids`            | set<string> | The node declares model or provider usage.                                     |
| `model_families`          | set<string> | The node's declared models resolve to families in the library's model catalog. |

The three sets let you block a node for the models it binds to, before anyone runs it.

### `Project`

| Attribute | Type   | Present when                                                                |
| --------- | ------ | --------------------------------------------------------------------------- |
| `id`      | string | Always. The project's opaque id, a GUID for projects created in the editor. |
| `name`    | string | The template has loaded far enough to know its name.                        |

Do not build a project `id` yourself or assume it is a file path. It is matched verbatim, and the editor generates it. Copy it from the project picker in the Permission Editor. Older projects that predate explicit ids fall back to their file path, so the id space is mixed.

Match on `name` for a rule a human can read, and on `id` when you need to be exact. Prefer `id` in a `permit`: `name` resolves only once the template has loaded, and a permit that never matches is a denial.

### `Model`

| Attribute        | Type        | Present when                                                         |
| ---------------- | ----------- | -------------------------------------------------------------------- |
| `id`             | string      | Always. The stable catalog model key.                                |
| `provider_id`    | string      | The key resolves in the model catalog.                               |
| `model_families` | set<string> | The resolved model declares a family. Carries that one family.       |
| `node_type`      | string      | An `OfferModel` check made on behalf of a node. Names the node type. |

`node_type` is filled at runtime but is not part of the declared entity model, so guard it and expect it to move. Everything else in the table is stable.

A `Model` also sits under its provider in the entity hierarchy, so `resource in ModelProvider::"anthropic"` covers every model that provider offers.

### `VideoCodec`

| Attribute          | Type   | Present when                                           |
| ------------------ | ------ | ------------------------------------------------------ |
| `id`               | string | Always. The codec name as probed, e.g. `h264`, `hevc`. |
| `container_format` | string | The container was known, e.g. `mp4`, `mov`.            |

## Context facts

Every context fact is optional. The engine fills what it can resolve and omits the rest, so guard each one with `has`.

| Fact                     | Type        | Notes                                                                      |
| ------------------------ | ----------- | -------------------------------------------------------------------------- |
| `active_project.id`      | string      | Canonical key of the project the engine is working in.                     |
| `active_project.name`    | string      | Its display name, when the template has loaded. Needs its own `has` guard. |
| `engine.id`              | string      | The active engine's id.                                                    |
| `loaded_libraries.names` | set<string> | Names of the libraries loaded so far.                                      |
| `license_id`             | string      | The license key's id.                                                      |
| `org_id`                 | string      | The organization the license belongs to.                                   |
| `license_type`           | string      | Either `"headless"` or `"interactive"`.                                    |

`loaded_libraries.names` grows as libraries load, so a rule reading it during boot sees a partial list. Treat it as advisory.

## Scoping a rule to one project

A Project-scoped builder template gates every statement it emits on the linked project:

```cedar
when { context has active_project && context.active_project.id == "<project id>" }
```

Two things follow from how the engine binds `active_project`.

**Ancestors count.** When a project inherits from a parent, the policy runs once per project in the chain: the active project, then each ancestor. Every run must allow, so a `forbid` scoped to a parent blocks its children, and a project allow-list has to name every project in the chain.

**The blank canvas is exempt from allow-list misses.** Before you open a project, the engine sits on a blank canvas whose project id is `<system-defaults>`. An allow-list that never names it still permits work there, otherwise the engine could not load a single library at boot. An explicit `forbid` on `<system-defaults>` is still honored, so guardrails keep working on the blank canvas. The exemption does not extend to opening a project *from* the blank canvas: that is evaluated against the project you are opening, so an allow-list still decides which projects are reachable.

## Annotations

Cedar tells the engine *which* rule denied something, not what the user is missing. Three annotations close that gap. All three are a Griptape convention, and Cedar ignores them during evaluation.

| Annotation              | Purpose                                                                         |
| ----------------------- | ------------------------------------------------------------------------------- |
| `@id("<slug>")`         | Stable name for the rule. Shown in the denial instead of a positional fallback. |
| `@capability("<name>")` | What the user lacks, e.g. `arbitrary-code-execution`.                           |
| `@advice("<text>")`     | What to do about it. This is the sentence the user reads.                       |

```cedar
@id("nodes/no-arbitrary-code")
@capability("arbitrary-code-execution")
@advice("Nodes that run arbitrary code are not available on this license. Ask your studio admin to enable them.")
forbid(principal, action == Action::"InstantiateNode", resource)
when { resource has executes_arbitrary_code && resource.executes_arbitrary_code };
```

Annotate every `forbid` you write. Without `@advice` the user gets a policy id and no way to act on it. Spell the keys exactly: Cedar treats a misspelled annotation as just another opaque tag, so `@advise` is silently ignored.

`@capability` is free-form, so nothing checks the name. Reuse the ones the builder emits and denials stay consistent across templates: `library`, `library-lifecycle`, `node-lifecycle`, `arbitrary-code-execution`, `project`, `model`, `model-provider`, `model-family`, `video-codec`.

## Examples

Each example is a statement to put in a template, not a whole policy. A `forbid` example assumes something already supplies the catch-all `permit`, either the same template's Exploration posture or another template on the same license key. Put a `forbid` in a template that permits nothing and it denies far more than it names. A `permit` example is the reverse: it belongs in a deny-by-default Production template.

### Block nodes that run arbitrary code

Shown with the catch-all permit, so this one statement is a complete blocklist template:

```cedar
permit(principal, action, resource);

@id("nodes/no-arbitrary-code")
@capability("arbitrary-code-execution")
@advice("Nodes that run arbitrary code are not available on this license.")
forbid(principal, action == Action::"InstantiateNode", resource)
when { resource has executes_arbitrary_code && resource.executes_arbitrary_code };
```

### Block unstable libraries and nodes

Two statements, because a denied library and a denied node lack different capabilities and the denial should say which:

```cedar
@id("lifecycle/no-experimental-libraries")
@capability("library-lifecycle")
@advice("Experimental libraries are blocked on this license. Ask your studio admin which libraries are approved.")
forbid(principal, action == Action::"LoadLibrary", resource)
when {
  resource has lifecycle_stage &&
  (resource.lifecycle_stage == "LABS" || resource.lifecycle_stage == "ALPHA")
};

@id("lifecycle/no-experimental-nodes")
@capability("node-lifecycle")
@advice("Experimental nodes are blocked on this license. Use a STABLE or BETA alternative.")
forbid(principal, action == Action::"InstantiateNode", resource)
when {
  resource has lifecycle_stage &&
  (resource.lifecycle_stage == "LABS" || resource.lifecycle_stage == "ALPHA")
};
```

The stages are `STABLE`, `BETA`, `ALPHA`, `LABS`, and `DEPRECATED`.

### Allow only two model providers

`unless` inverts the match, so this blocks every provider except the two named. `in` never errors on a resource with no such ancestor, so it needs no `has` guard:

```cedar
@id("models/approved-providers")
@capability("model-provider")
@advice("Only Anthropic and OpenAI models are approved on this license.")
forbid(principal, action in [Action::"OfferModel", Action::"InvokeModel"], resource)
unless { resource in ModelProvider::"anthropic" || resource in ModelProvider::"openai" };
```

### Block a model family

`model_families` is a set, so match it with `contains`:

```cedar
@id("models/no-claude-3")
@capability("model-family")
@advice("Claude 3 models are retired on this license. Use Claude 4.")
forbid(principal, action in [Action::"OfferModel", Action::"InvokeModel"], resource)
when { resource has model_families && resource.model_families.contains("Claude 3") };
```

### Allow-list projects

Deny-by-default style, with no catch-all permit. Both project checkpoints are named, since loading and activating are gated separately:

```cedar
@id("projects/approved")
@capability("project")
@advice("This project is not on your license. Ask your studio admin for access.")
permit(principal, action in [Action::"LoadProject", Action::"ActivateProject"], resource)
when {
  resource has id &&
  (resource.id == "8f2c1a04-9d3e-4b7a-9f10-2c5d6e8a1b33" || resource.id == "c07b5e91-4a2d-4f88-bd63-1e9f7a205c48")
};
```

### Restrict a codec for writing only

Only the write checkpoint is named, so this statement leaves reading existing footage untouched:

```cedar
@id("video/no-prores-writes")
@capability("video-codec")
@advice("Writing ProRes is not permitted on this license. Render to H.264 instead.")
forbid(principal, action == Action::"WriteVideoCodec", resource)
when { resource has id && resource.id == "prores" };
```

### Apply a rule only inside one project

Cedar ANDs multiple `when` clauses, so the project gate and the rule read as two separate conditions:

```cedar
@id("show-a/no-experimental-nodes")
@capability("node-lifecycle")
@advice("Show A is locked to stable nodes.")
forbid(principal, action == Action::"InstantiateNode", resource)
when {
  context has active_project &&
  context.active_project has name &&
  context.active_project.name == "Show A"
}
when { resource has lifecycle_stage && resource.lifecycle_stage == "LABS" };
```

### Restrict headless licenses

```cedar
@id("headless/no-model-invocation")
@capability("model")
@advice("Headless licenses cannot call models on this plan.")
forbid(principal, action == Action::"InvokeModel", resource)
when { context has license_type && context.license_type == "headless" };
```

## Gotchas

### Guard every optional attribute

Reading an attribute that is not there makes the expression error. Cedar treats an errored condition as unsatisfied, and the engine denies anything it could not evaluate cleanly. So an unguarded read does two bad things at once: it lets a `forbid` fall through when the attribute is missing, and it blocks legitimate operations that simply do not carry that fact.

```cedar
// Wrong. Denies every node type with no declared lifecycle stage.
forbid(principal, action == Action::"InstantiateNode", resource)
when { resource.lifecycle_stage == "LABS" };

// Right.
forbid(principal, action == Action::"InstantiateNode", resource)
when { resource has lifecycle_stage && resource.lifecycle_stage == "LABS" };
```

`&&` short-circuits, so the guard has to come first. The one exception is `in`, which returns false instead of erroring when the resource has no such ancestor.

Nested facts need their own guard. `context has active_project` says nothing about whether that project resolved a `name`:

```cedar
when {
  context has active_project &&
  context.active_project has name &&
  context.active_project.name == "Show A"
}
```

### Typos in names

Saving a Raw Cedar template parse-checks it, so a template with a syntax error cannot be saved. It does not check that `resource.lifecycle_stge` is a real attribute or that `Action::"LoadLibrry"` is a real checkpoint. A typo like that parses cleanly and then matches nothing, which reads as a rule that silently does not work. Copy attribute and action names from the tables above.

### Hierarchy limits

`resource in ModelProvider::"anthropic"` resolves, because the engine attaches a model's provider edge. Two shapes that look similar do not resolve:

- `resource in ModelFamily::"Claude 4"` matches nothing. Use `resource.model_families.contains("Claude 4")`.
- `resource in Library::"My Library"` matches nothing on a `NodeType`. Match the node's `id`, or gate the library with `LoadLibrary`.

### One permit per checkpoint

A Production template that permits only `LoadLibrary` leaves node instantiation, project loading, model use, and codec use to default deny. If it is the only template on a license key, nothing works. Either name every checkpoint the user needs or pair the template with another that supplies the rest.

### Engine-tier enforcement

The Permission Editor stores every template tagged `enforce_at: ["engine"]`, which is the tier that can see resolved libraries, node types, projects, models, and codecs. Do not hand-edit the tag.

Engine-tier policy is a guardrail, not a tamper-proof boundary. It runs inside the engine process on the user's machine, so treat it as the mechanism that keeps a cooperative user inside approved bounds, not as a defense against someone determined to work around it. License-critical rules are enforced ahead of the engine and are not authored here.

## Tracing a denial back to a rule

A denial reads as the rule's `@advice`, followed by the template and rule that produced it:

```
Nodes that run arbitrary code are not available on this license. (source: policy 'Show A Lockdown', rule 'nodes/no-arbitrary-code').
```

The policy name comes from the template, and the rule name from its `@id` annotation. A rule with no `@id` falls back to the document's own id, which for a document that never declared one is a positional `license-<n>` that shifts as documents are added or reordered. That is the main reason to write `@id` on everything. A rule with no `@advice` leads with `Missing capability: <name>.` from `@capability`, or `Denied by the license policy.` when it carries neither.

A clause ending in `managed by Griptape` marks a Griptape-authored rule, so you cannot edit its text. It does not tell you whether the rule is detachable. A Griptape-managed template your admin attached can be detached in the Permission Editor; a policy Griptape applies platform-wide cannot.
