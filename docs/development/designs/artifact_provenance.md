# Design: Artifact Provenance

Tracking issue: [griptape-nodes-engine#5626](https://github.com/griptape-ai/griptape-nodes-engine/issues/5626).
Editor follow-up (iteration view): [griptape-nodes-engine#5627](https://github.com/griptape-ai/griptape-nodes-engine/issues/5627).

## 1. Motivation and prior art

Provenance answers two families of questions:

- **The artist's**: "Three runs ago the character was left-handed. How did I frame that prompt?" — even when every run overwrote the same output file.
- **The studio's**: "What model, workflow, and inputs produced this deliverable?"

The engine has a deliberate but narrow provenance system today, and it cannot answer either question reliably:

| Existing mechanism                                                                             | Shortfall                                                                                                                                                                                                                |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Sidecar JSON per saved file (`.griptape-nodes-metadata/`, `sidecar_metadata.py`, schema 0.2.0) | **Mutable** — overwritten on every save, so re-saving a path erases all prior iterations. No read API. The macro lacks a drive/volume segment, so two outside-workspace files with the same name collide on one sidecar. |
| Parameter capture via `_collect_raw_provenance()` (`workflow_metadata.py`)                     | Captures `resolving_nodes[0]` of the current flow — an **inference** that picks the wrong node in parallel or multi-branch flows.                                                                                        |
| PNG text-chunk auto-injection (`gtn_` keys incl. `gtn_flow_commands`, a pickled flow)          | PNG-only (Flux and OpenAI image nodes default to JPEG); video/audio/3D get nothing; the pickled-flow-in-image payload bloats files, is stripped by any re-encode, and unpickles foreign data.                            |
| Cloud storage backend                                                                          | Skips **all** metadata injection and sidecars — cloud-stored artifacts have zero provenance.                                                                                                                             |
| Lineage                                                                                        | **None.** Feeding image X into a generation that produces Y records nothing about X. Artifact `meta` dicts exist in memory but are dropped at nearly every URL↔bytes conversion between nodes.                           |

This design replaces the sidecar system with an immutable, lineage-aware, queryable record store. The PNG `gtn_` injection path is untouched here; it is listed as a separate deprecation candidate (§13).

## 2. Settled decisions

These were ruled during design review and are recorded so they are not relitigated:

1. Capture is elected **per save** via a `StrEnum` policy (never a bool) with a project-level default. The engine ships `FULL_WORKFLOW_SNAPSHOT` as the default. There is always a project context (the default project template applies when none is specified), so inheritance always terminates.
1. Failure handling is a **separate policy axis**. Default: an elected record that cannot be written **fails the artifact save**.
1. Records are **immutable per-save-event files**, never overwritten. Iteration history — including overwriting the same output path — falls out for free. Canonical history is never cleared by UI actions; a "reset" of the artist-facing view is an editor-side cursor (#5627). Users deleting record files by hand is their prerogative; readers tolerate it.
1. Records live in a **centralized, preview-style mirrored store** in a **visible** (not dot-hidden) directory — provenance is canonical work product customers will commit and ship, unlike regenerable preview caches.
1. Each record is **two-layer**: a human-readable envelope plus a faithful payload produced by the engine's existing serialization machinery (many parameter types cannot be represented as plain text). Revised 2026-09-23: the payload is the FULL serialize-node protocol (commands + indirect set-value commands + pickled unique values) — the readable `parameters_preview` is an explicitly lossy display projection, never the truth.
1. Source links are **hybrid**: a precise reference `(path_at_use, record_id, content_hash)` plus an embedded one-line ancestor summary that survives even if the ancestor's record is later deleted.
1. Lineage attaches at **file boundaries**. The standard library passes media between nodes exclusively as file-backed URL artifacts, so this covers real flows; a third-party node that hands raw bytes downstream without a save creates a documented chain break.
1. **Out of scope**: C2PA/signing, databases or index files (plain files + scans only), and embedding anything in file-format metadata (EXIF/PNG chunks).
1. The new store **subsumes** the old sidecar system (§11).
1. Records are **dual-addressed**: canonical by path, with a lightweight by-hash pointer, so moved/renamed/hand-copied files regain their provenance via content hash.

## 3. Concepts and vocabulary

- **Record** — one immutable YAML file (with in-band comments) describing one save event of one artifact.
- **Record ID** — time-sortable unique identifier; also the record's filename.
- **Central store** — the visible `griptape-nodes-provenance/` directory holding all records and pointers for a project.
- **Workflow snapshot** — the full text of a runnable workflow `.py` file, embedded in the record's payload at `FULL_WORKFLOW_SNAPSHOT`.
- **Source link** — a record's reference to a source artifact's record: the lineage edge.
- **Chain break** — a source link whose `record_id` is null because the input has no discoverable record (never saved through the engine, pre-feature file, or cross-project input).
- **Capture policy / failure policy** — the two per-save knobs (§6).
- **Canonical history vs. iteration view** — the record store is canonical and append-only; the artist-facing scoped view ("what have I made with this node since I switched tasks") is an editor concern built on the query API (#5627).

## 4. Record store layout

All provenance lives under one visible root, resolved through the project-template situation system:

```text
griptape-nodes-provenance/
├── by-path/                                  # canonical records, mirrored by source path
│   └── renders/
│       └── hero.png/                         # one directory per artifact file
│           ├── 20260921T182655120044Z-77be01c9.yaml
│           └── 20260921T183012482913Z-a3f9c2d1.yaml
├── by-hash/                                  # pointer files, addressed by content hash
│   └── 9f/
│       └── 9f2c…{full hash}…/
│           └── 20260921T183012482913Z-a3f9c2d1.yaml   # content: relative path to canonical record

```

### The situation macro

A new `BuiltInSituation.SAVE_ARTIFACT_PROVENANCE` is added to `common/project_templates/situation.py` and declared in the **v1 default template only** (mirroring how `SAVE_GRIPTAPE_NODES_PREVIEW` is declared). The v0 default is a frozen legacy baseline whose layout must never shift under existing projects — a new visible directory (and a hard-fail default on saves) is exactly that kind of shift. A template with no provenance situation (any v0/legacy project) resolves every capture to `NO_PROVENANCE_RECORDED` with a one-time log: legacy projects record nothing until they upgrade majors; they never start failing saves.

```text
{griptape-nodes-provenance}/by-path/{drive_volume_mount?:/}{source_relative_path?:/}{source_file_name}/{provenance_record_id}.yaml
```

with `on_collision=FAIL` (record IDs never collide; a collision is a bug that must surface) and `create_dirs=True`. The `{griptape-nodes-provenance}` directory definition is `{workflow_dir?:/}griptape-nodes-provenance`, matching the v1 preview convention.

- **Outside-workspace files** mirror under the `{drive_volume_mount}` segment exactly as previews do (`C:\temp\x.png` → `by-path/C:/temp/x.png/…`), which is what the old sidecar macro lacked — its same-name collisions are retired by this design.
- **The mirroring anchor is the configured workspace root** (what `StaticFilesManager` anchors to), not the project file's directory: anchoring to `project_base_dir` mirrored in-workspace saves as absolute paths (`by-path/Users/...`) whenever the project file lived outside the workspace root.
- **Resolve-time variables** include both the decomposed source variables *and* `artifact_dir` (the artifact's own parent directory), so a studio can flip the macro to an output-adjacent layout with a one-line template edit. Readers (source discovery, queries) resolve record locations through the same situation machinery — always possible because a project context always exists. Overriding the macro to a shape readers cannot mirror is supported at-your-own-risk, the same posture as custom preview macros.

### Why centralized (and why visible)

Centralized: output/delivery directories stay clean; history queries glob one root instead of walking the workspace; the store is a single unit to commit, back up, or clear; and previews already proved the mirroring scheme. The costs, accepted and documented: records do not travel inside a zipped output tree (the project packager can include the central store when provenance should ship), and inputs from *another* project's workspace resolve to the consuming project's store and appear as chain breaks.

Visible: customers will commit these files and build tooling against them; zip GUIs, sync tools, and artists routinely skip dot-directories. A side effect worth noting: the store shows up in generic file listings and is servable by the static server — which lets the editor fetch records over HTTP like any other workspace file.

### Dual addressing: by-path + by-hash

The canonical record lives in `by-path/`. Every record write also drops a tiny **pointer file** at `by-hash/{first two hex}/{full content hash}/{record_id}.json`, whose content is the relative path to the canonical record. Rationale:

- Path is where humans look, but it is the **least stable identity** — and out-of-project paths rot fastest. Content hash survives rename, move, and out-of-band (Finder) copies.
- On a path miss, lookups hash the file and consult `by-hash/` — moved, renamed, and hand-copied files regain their provenance.
- One hash can map to many records (same bytes saved repeatedly), hence a directory per hash; reusing the record ID as the pointer filename keeps hash-side listings time-sorted too.
- A pointer, not a duplicate record: full duplication would double every write and create two copies that can drift across schema migrations.

Why by-path holds the canonical record and by-hash points at it, not the inverse (design review, 2026-09-23): a record is keyed by **save event**, not content — content hash is many-to-many with records (one hash ↔ many saves of identical bytes; one path ↔ many iterations), so neither tree owns identity; the record ID does. Two things decide which tree holds the real file. First, the overwrite requirement kills hash-first: an overwritten iteration's bytes no longer exist anywhere, so "this file's history" is only answerable through the path — a hash-canonical store would still need the full path→records mapping as pointers, putting pointer-chasing on the hot path (every history browse, every source discovery) while giving the direct hit to the rare rename-recovery path. Second, the store is visible, committable work product: `by-path/renders/hero.png/` is Finder-browsable and its listing IS the timeline; a hash-keyed tree is opaque without tooling and groups unrelated files whenever bytes collide. (Git is not a counterexample — git never answers path queries without its own path index; trees and commits are its by-path layer.)

Pointer writes are covered by the same failure policy as the record. A missing pointer degrades to path-only lookup; a stale pointer (user deleted the canonical record) reads as record-missing — the same tolerance as chain breaks.

## 5. Record schema

### Record ID

```text
20260921T183012482913Z-a3f9c2d1
└──── UTC, µs precision ────┘ └ 8 hex chars of os.urandom ┘
```

Generatable at write time with zero coordination; lexicographic order equals chronological order, so "latest record" is the lexicographic max of a directory listing and no index is ever needed. The ID **is** the filename, so resolving a source link is a direct file open, never a scan. (UUIDv7 was rejected: extra dependency, and the hand-rolled form is readable in `ls` output.)

### Envelope

One JSON document per record, `schema_version: "1.0.0"` (independent of the old sidecar's 0.2.0):

```jsonc
{
  "schema_version": "1.0.0",
  "record_id": "20260921T183012482913Z-a3f9c2d1",
  "saved_at": "2026-09-21T18:30:12.482913+00:00",
  "capture_policy": "full_workflow_snapshot",       // always the RESOLVED policy, never "inherit"
  "relationship": "produced",                       // "produced" | "copied_from"
  "engine_version": "0.63.0",
  "summary_line": "TextToImage_1 [FluxGenerate, flux-lib 1.2.0] in 'hero-pipeline' — prompt='castle at dawn…' → renders/hero_1.png",

  "artifact": {
    "final_path": "/Users/x/ws/renders/hero_1.png", // post collision-walk, post extension-coercion
    "workspace_relative_path": "renders/hero_1.png",// null when outside the workspace
    "file_name": "hero_1.png",
    "content_hash": "blake2b-256:9f2c…",            // hash of the exact bytes written
    "size_bytes": 1048576,
    "append": false,
    "requested_path": "renders/hero.png",           // pre-walk intent; null if identical
    "extension_coerced_from": null                  // e.g. "jpg" when sniff-and-swap renamed the file
  },

  "producing_node": {
    "name": "SaveImage_1",
    "node_type": "SaveImage",
    "library_name": "Griptape Nodes Library",
    "library_version": "0.41.2",
    "identity_source": "explicit"                   // "explicit" | "inferred_resolving_node"
  },

  "parameters_preview": { "prompt": "castle at dawn", "steps": 30 },  // LOSSY display projection; payload is truth
  "parameters_omitted": ["api_key_override"],       // exclude_from_metadata parameters, named but not valued

  "workflow": {                                     // null when the workflow has never been saved
    "name": "hero-pipeline",
    "flow_name": "hero-pipeline",
    "created": "…",
    "modified": "…",
    "engine_version_created_with": "0.62.0"
  },

  "package": {                                      // null unless the project came from a published package
    "source_project_id": "…",
    "package_project_name": "…",
    "exported_at": "…"
  },

  "situation": {                                    // null for absolute-path saves (see §7)
    "name": "save_file_in_project",
    "macro": "{outputs}/…",
    "policy": { "on_collision": "create_new", "create_dirs": true },
    "variables": { "file_name_base": "hero", "file_extension": "png", "node_name": "SaveImage_1" }
  },

  "sources": [
    {
      "path_at_use": "/Users/x/ws/inputs/gen_004.png",
      "parameter_name": "image",
      "record_id": "20260921T182655120044Z-77be01c9",  // null == chain break
      "content_hash": "blake2b-256:41aa…",             // the input's bytes AS CONSUMED
      "matches_latest_record": true,                   // false → the input changed after its last record
      "summary": "…copied verbatim from the parent record's summary_line…"
    }
  ],

  "payload": {
    "serialized_node": {                               // presence = the node is rebuildable
      "serialized_node_commands": { /* readable structure */ },
      "set_parameter_value_commands": [ /* indirect commands keying into the pickled values */ ],
      "pickled_parameter_values": "<base64 pickle>",   // engine-only; the unique-values dict
      "parameter_values_format": "gtn-unique-values-pickle-v1"
    },
    "serialized_workflow": {                           // presence = FULL_WORKFLOW_SNAPSHOT
      "workflow_file_content": "…full runnable workflow .py text (block scalar in YAML)…",
      "workflow_file_hash": "blake2b-256:b17e…"
    }
  }
}
```

Notes:

- **Content hashing is effectively free**: the write pipeline holds the full byte content in memory at capture time; `blake2b` over in-memory bytes costs microseconds-to-milliseconds. The hash covers the bytes *as written* (after any in-band metadata injection).
- **`summary_line`** is computed once, when the record is written. Children copy a parent's `summary_line` verbatim into their source links — the hybrid summary costs O(1) per save instead of re-deriving ancestor descriptions.
- **Parameters** use the existing readable-summary path (`_collect_parameter_values` in `workflow_metadata.py`, `safe_unstructure`, honoring each parameter's `exclude_from_metadata` flag). Omitted parameters are *named* in `parameters_omitted` so redaction is visible, not silent.
- The **hash X/Y problem** is solved structurally: if file A (hash X) fed the generation of file B, and A is later re-saved with different content (hash Y), B's source link still names A's *specific immutable record*. Drift is detectable — a reader comparing A's current bytes to the linked record's hash can flag "this input has changed since generation," which is a feature for artists, not just a failure mode.

### Payload: FULL is a strict superset

The envelope is identical at every capture level; only `payload` grows, so lowering a project's policy never changes record shape:

- **`PRODUCING_NODE_ONLY`** → the producing node captured via the FULL serialize-node protocol: a fresh `unique_parameter_uuid_to_values` dict + `SerializedParameterValueTracker` passed into `SerializeNodeToCommandsRequest` with `use_pickling=True` and `serialize_all_parameter_values=True`. The record stores the structural commands (readable JSON), the indirect set-value commands (readable), and the pickled unique-values dict (base64, engine-only). This matters: the values dict is filled *in-place on the request* — a bare serialize call (the original implementation bug) records commands whose value references point at a dict that was never captured, and cannot rehydrate.
- **`FULL_WORKFLOW_SNAPSHOT`** → everything above **plus the entire workflow embedded as the text of a runnable `.py` workflow file** (`payload.workflow_file_content`, rendered by `WorkflowManager.render_workflow_file_content` — the same codegen a workflow save uses, without touching disk or the registry). The `.py` format is the engine's one versioned, migration-supported container (PEP-723 header with `schema_version`, `engine_version_created_with`, library version pins): extract the string to disk and any engine loads it years later. A raw `SerializedFlowCommands` pickle was rejected for this role — no format version, no migration path. Rendering runs per capture (no memo): a stale cached snapshot would embed the WRONG workflow after a mid-session edit, and correctness beats the codegen cost.

Why the snapshot is embedded rather than a separate content-addressed file (design review revision, 2026-09-23 — supersedes an earlier separate-file ruling): once the payload carries a pickled values blob regardless, the record is already a hybrid document and the "keep the envelope readable" argument for splitting dies; what remains is intra-run dedupe versus **full self-containment** — one file per save event, no dangling snapshot references, no partial-store copies losing snapshots, and "does the workflow snapshot exist?" becomes "is the field present." Self-containment wins. Accepted costs: multi-save runs duplicate the workflow text per record (typically tens-to-hundreds of KB), and the pickled-literal spans of generated workflow code are escape-heavy inside a quoted string — mitigated by the YAML block scalar (§ record format below). `workflow_file_hash` is retained for integrity and cross-record dedupe *detection*.

### Record format: YAML with in-band comments

Records and by-hash pointers are **YAML**, not JSON (ruled 2026-09-23). Two reasons:

- **Comments for a future inspector.** Every record carries a fixed per-schema-version comment template — what the file is, that the payload is the only faithful representation, that the preview is lossy, how to use the embedded workflow file. A human or agent opening a record cold gets its interpretation guidance co-located, not one link away.
- **Block scalars for the embedded workflow.** `workflow_file_content` dumps as a literal block scalar: the workflow `.py` stays readable line-by-line (header TOML skimmable in place) instead of one escape-laden quoted string.

Dump conventions mirror the project-template YAML (`build_project_yaml`): every plain string double-quoted so YAML 1.1 coercions (the Norway problem) can never bite; reads go through `yaml.safe_load` → `model_validate`. Accepted cost: YAML parses slower than JSON, felt in query-API scans and bounded by the filename-timestamp prefilter. (A store-root README was tried and removed: with commented records it was redundant, and it added a write path with its own failure modes in read-only environments.)

### Schematization and the external contract

Pydantic models in a new `retained_mode/file_metadata/provenance_record.py`: `ProvenanceRecord`, `ArtifactIdentity`, `ProducingNodeIdentity`, `ParentLink`, `ProvenancePayload`, `WorkflowIdentity`, `PackageIdentity`. Written via `dump_record_yaml` (commented YAML), validated on read with `load_record_yaml` → `model_validate`.

Versioning: readers accept any `1.x`. Additive fields bump the minor version; a breaking shape change bumps the major version and readers surface "this record was written by a newer engine" rather than guessing.

Because customers will commit these files and build pipelines against them, the record schema is a **supported external contract**: the generated JSON Schema (pydantic emits it) is published in the docs per `schema_version` and validates the record's *data model* (the on-disk format is YAML; YAML's data model maps cleanly onto it). External tools can read everything except the pickled payload fields, which are declared opaque and engine-only. Known aging characteristic, documented rather than solved: pickled values have no migration story across engine versions — bounded exposure (the commands layer is version-tolerant JSON, and the embedded workflow `.py` is the durable full-fidelity fallback).

## 6. Policies and the resolution ladder

```python
class ProvenanceCapturePolicy(StrEnum):
    INHERIT_PROJECT_POLICY = "inherit_project_policy"
    NO_PROVENANCE_RECORDED = "no_provenance_recorded"
    PRODUCING_NODE_ONLY = "producing_node_only"
    FULL_WORKFLOW_SNAPSHOT = "full_workflow_snapshot"


class ProvenanceFailurePolicy(StrEnum):
    INHERIT_PROJECT_POLICY = "inherit_project_policy"
    FAIL_ARTIFACT_SAVE = "fail_artifact_save"  # engine default
    WARN_AND_CONTINUE = "warn_and_continue"
```

Capture defaults are a **per-artifact-type table** (ruled 2026-09-23, superseding an earlier binary format gate). Artifact types are the provider registry's vocabulary — a type's key is the claiming provider's friendly name, lowercased — so new providers (a future 3D or text provider) become addressable the day they register:

```yaml
provenance:
  default_failure_policy: fail_artifact_save
  per_artifact_type:            # THE capture policy: not in the list => no provenance
    image: full_workflow_snapshot
    video: producing_node_only
```

The table is the whole capture story (ruled 2026-09-23): a type not in the list records nothing, and formats no provider claims can never be in a kind-keyed list, so they never record via inherit (the "valid artifact" posture; field testing showed an everything-records posture fills stores with workflow-`.py` and scratch noise). The field *defaults* to the engine table (`image`/`video`/`audio` → `FULL_WORKFLOW_SNAPSHOT`), so a block that only sets failure policy does not silently kill capture; specifying `per_artifact_type` replaces it wholesale. On the request surface, a policy of `None` is the idiomatic spelling of inherit (`INHERIT_PROJECT_POLICY` remains a valid explicit form). **The table governs `INHERIT` resolution only: an explicit per-save election always wins** (ruled 2026-09-23) — every first-party supplier sends `INHERIT`, so the table covers all real traffic while a node author's deliberate election is honored. Failure policy stays global; per-type failure semantics has no story behind it. The completing follow-up is a minimal 3D artifact provider (and possibly text), so `.glb`/`.usdz` generation outputs become recognized types instead of relying on `unrecognized_capture_policy`.

Two location/purpose exclusions ride alongside the table (both ruled from field testing, 2026-09-23):

- **Engine scratch space**: a save landing under the OS temp root *and outside the workspace* never captures — staging intermediates are transient by definition, so their records would be guaranteed-dangling. A workspace deliberately placed under temp (ephemeral/CI) still captures, and user-chosen out-of-workspace destinations capture normally.
- **Ingestion copies**: normalization uploads (reference inputs copied into `staticfiles/` for a stable served URL) pass an explicit `NO_PROVENANCE_RECORDED` election — the copy is the engine normalizing an INPUT, not producing an artifact. The consumer's source link still records the copy's path and content hash with `record_id: null`: a documented ingestion boundary rather than a lineage record.

Project defaults live on the **project template** (a new optional `ProvenanceSettings` block on `ProjectTemplate`: `default_capture_policy`, `default_failure_policy`), not on a config key — policy must travel with projects and published packages, and the template overlay/merge machinery already exists. The default templates do **not** instantiate the block (no other optional settings block in the templates is instantiated with its own defaults); it exists only for projects that set it, and its fields reject `inherit_project_policy` since the block is what saves inherit *from*.

Resolution ladder: request value → (if inherit) project template block → (if absent) engine constants (`FULL_WORKFLOW_SNAPSHOT`, `FAIL_ARTIFACT_SAVE`). Independent of the ladder, a template with no provenance *situation* (v0/legacy) resolves to `NO_PROVENANCE_RECORDED` — see §4.

**Published workflows**: the *running* project's policy governs capture — a publisher "pin" would be unenforceable theater given no trust layer, and would surprise consumers. What the published case changes is record *content*: when the project came from a package, records carry the package identity block (`source_project_id`, name, export date) so lineage reads "generated by published workflow P v1.2."

## 7. Capture pipeline

### Request plumbing

A new `ProvenanceContent` model rides the write request:

```python
class ProducingNodeIdentity(BaseModel):
    node_name: str
    node_type: str
    library_name: str | None = None
    library_version: str | None = None


class ProvenanceContent(BaseModel):
    capture_policy: ProvenanceCapturePolicy = ProvenanceCapturePolicy.INHERIT_PROJECT_POLICY
    failure_policy: ProvenanceFailurePolicy = ProvenanceFailurePolicy.INHERIT_PROJECT_POLICY
    producing_node: ProducingNodeIdentity | None = None  # None → inferred, flagged in the record
    situation: SituationMetadata | None = None  # subsumes SidecarContent.situation
```

`WriteFileRequest` and `CopyFileRequest` gain `provenance: ProvenanceContent | None = None`. **`None` means no capture at all**, mirroring today's `file_metadata` opt-in. This one rule keeps internal writes — previews, temp files, and the record/pointer writes themselves — out of the system with no recursion guard.

Suppliers:

- **`ProjectFileParameter.build_file()`** already knows its node; it constructs an explicit `ProducingNodeIdentity` and threads a `ProvenanceContent` through `ProjectFileDestination.from_situation` → `File` → `WriteFileRequest`. This is what retires the `resolving_nodes[0]` guess on the main save path.
- **`StaticFilesManager.save_static_file`** gains a `provenance` parameter. Ingestion uploads (`normalize_artifact_input`) pass `None` — normalizing an input is not producing an artifact.
- **Inference fallback**: when `producing_node` is `None` but capture is elected, the existing resolving-node inference applies and the record is stamped `identity_source: "inferred_resolving_node"` so consumers can distinguish attested identity from guessed identity.
- **Absolute-path saves**: today `ProjectFileDestination` drops all metadata for absolute paths to avoid "a dishonest provenance trail." Under this design only the `situation` block was dishonest — the record itself (real final path, real hash, real node) is honest, so absolute-path saves capture normally with `situation: null`.

### Hook placement and failure ordering

Capture runs inside `OSManager.on_write_file_request`, where the old sidecar call sits today — the only place the **final** path (post collision-walk, post extension-coercion) and the exact written bytes are both known.

Strategy: **artifact first, record second, roll back on record failure** — with two refinements:

- **OVERWRITE** (atomic temp-then-rename): the record is written *inside* the atomic window — stage the temp file, write the record, then rename. Record failure ⇒ delete the temp; the prior artifact is untouched and no record exists for a save that never happened. (Requires splitting the atomic write into stage/commit steps; small, contained change.)
- **FAIL / CREATE_NEW** (exclusive-create modes): on record failure, unlink the file we exclusively created and fail the request.
- **APPEND** cannot be rolled back, so under `FAIL_ARTIFACT_SAVE` a pre-flight check (record directory creatable) runs *before* any bytes are appended.

Record-before-artifact universally was rejected: `CREATE_NEW`'s final path does not exist until the exclusive create succeeds, and orphan records describing saves that never happened are worse than missing records.

Failures surface as a new `FileIOFailureReason.PROVENANCE_WRITE_FAILED` with an artist-comprehensible message:

> Attempted to save '<file>' with a provenance record, but the record could not be written (<cause>). The save was rolled back because this project requires provenance. Change the project's provenance failure policy to 'warn_and_continue' to save without records.

`skip_metadata_injection` is orthogonal and unchanged — it governs in-band PNG-chunk injection only; provenance keys solely on the `provenance` field.

### Result surface

Callers get the capture outcome back on the write result rather than having to re-query for a record the engine just created. `WriteFileResultSuccess` (and `CopyFileResultSuccess`) gain an optional block:

```python
class ProvenanceWriteDetails(BaseModel):
    record_id: str
    record_path: str  # resolved canonical record path (or cloud key)
    content_hash: str
    capture_policy: ProvenanceCapturePolicy  # the RESOLVED policy, never "inherit"
    warning: str | None = None  # set when WARN_AND_CONTINUE swallowed a record failure
```

`provenance: ProvenanceWriteDetails | None = None` — `None` when no capture was elected or the policy resolved to `NO_PROVENANCE_RECORDED`. This is what lets a node expose the record ID as an output parameter, and lets the editor surface provenance immediately after a save without a follow-up query.

One capture writes two files (canonical record and by-hash pointer), but the details object reports only the canonical `record_path`: the pointer path is a pure function of `content_hash` + `record_id`. One save event always produces exactly one record — that is the immutability contract. `record_path` here is explicit (absolute path or cloud key) because the details object is a transient result consumed immediately, mirroring `final_file_path` beside it; the *persisted* files are where portability matters, and those use store-root-relative forms throughout.

When `WARN_AND_CONTINUE` swallows a full capture failure there is no record and therefore no details object; the warning is carried in the result's `result_details` (WARNING level) instead. The `warning` field on the details object is reserved for degraded-success cases where a record exists but a companion write partially failed.

### Concurrency

A per-record-directory `KeyedMutex` (the preview system's pattern), keyed on `canonicalize_for_identity` of the record directory, protects the read-latest-parent/write-record window. Record files themselves never contend — their names are unique.

## 8. Source discovery

At capture time, given the producing node:

1. **Enumerate** parameter values for parameters whose `allowed_modes` include INPUT or PROPERTY (the same filter the readable-parameter summary uses); flatten one level of lists/dicts.
1. **Classify file-backed values — strictly type-driven** (revised 2026-09-23; a design-review incident superseded the original rules):
    - Only values the type system declares to be artifacts are considered: URL artifacts (`ImageUrlArtifact` etc., static-server URLs mapping back to workspace paths; cloud asset URLs via the cloud driver's URL-to-path extraction), and typed file handles (`File`/`MacroPath`-bearing objects) when support lands.
    - **Strings classify only when the holding parameter is DECLARED as an artifact type** (revised again 2026-09-23 after field testing): the type system may testify through the value's runtime type *or* through the parameter declaration — a string sitting in a parameter declared `ImageUrlArtifact` is, by declaration, a file reference, while a string in a `str`-typed parameter is prose and is never probed. History matters here: the original design probed *all* strings gated by shape heuristics, and the existence probe itself proved unsafe (`stat()` on a multi-KB prompt raises `ENAMETOOLONG` instead of answering "not a file", rolling back a real save under `fail_artifact_save`); the first correction banned strings entirely, which field testing then showed erased lineage for real workflows whose reference images travel as path strings in image-declared parameters. The declared-type gate is the resolution: no value-shape sniffing, the probe stays exception-guarded (junk in a declared parameter degrades to "not a parent"), and prompts are structurally unreachable. Classification errors of any kind degrade, never fail capture.
1. **Resolve** each input path to its record directory through the same situation machinery writers use.
1. **Latest record** = lexicographic max of the directory. Extract `record_id`, `content_hash`, `size_bytes`, `summary_line`.
1. **Staleness**: compare the input file's current size to the record's; only if sizes match, hash the bytes. `matches_latest_record: false` means the input changed after its last record. The source link's `content_hash` is always the input's bytes *as consumed now*.
1. **No records at the path** → hash the input and try `by-hash/` before declaring a break — this recovers renamed and hand-copied inputs. Still nothing → emit the link with `record_id: null` (path and hash still recorded): the documented chain break.

Any single parent's discovery error (unreadable JSON, stat failure) degrades to a `record_id: null` link with an artist-readable `discovery_error` field. Source discovery never fails the save on its own — only record *writing* participates in the failure policy.

## 9. Query API

A new `ProvenanceManager` (extends `EngineScoped`, constructed by `Engine` like its ~28 peers; folding into the already-large `ArtifactManager` was rejected) handles a new event family in `retained_mode/events/provenance_events.py`:

| Request                                                                                                                                      | Answers                                                                  | Mechanics                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GetProvenanceForArtifactRequest(file_path, record_id=None, include_payload=False)`                                                          | "What made this file?"                                                   | Latest (or specific) record for the path; `is_stale` compares current bytes to the record hash; **hash fallback** on a path miss; `legacy_sidecar` fallback (§11).          |
| `ListProvenanceRecordsForArtifactRequest(file_path)`                                                                                         | "Every iteration of this file"                                           | Directory listing, newest first — the overwrite-same-file history.                                                                                                          |
| `ListProvenanceRecordsForHashRequest(content_hash)`                                                                                          | "Every record for these exact bytes"                                     | Direct `by-hash/` lookup, for tooling that already has the hash.                                                                                                            |
| `ListProvenanceRecordsRequest(producing_node_name=None, node_type=None, workflow_name=None, saved_after=None, saved_before=None, limit=200)` | The artist's iteration query ("everything TextToImage_1 made this week") | Glob of the single central root — no workspace walk. Record-ID filenames double as a **time-range prefilter**: files outside `saved_after`/`saved_before` are never opened. |
| `ResolveProvenanceChainRequest(file_path, record_id=None, max_depth=16)`                                                                     | "The whole ancestry"                                                     | Recurses source links; each hop is a direct file open (the ID is the filename). Visited-set cycle guard; breaks surface as explicit nodes.                                  |
| `ListDescendantsForArtifactRequest(file_path, record_id=None)`                                                                               | Reverse lineage: "which artifacts used this one?"                        | Central-root scan matching source links by path/record ID/hash — the impact question artists and studios both ask.                                                          |

Scans are O(records) by design (no index); the central root and filename prefilter keep them cheap for editor-driven browsing. Failure reasons are a `ProvenanceFailureReason` StrEnum (`ARTIFACT_NOT_FOUND`, `NO_PROVENANCE_RECORDS`, `RECORD_UNREADABLE`, `SNAPSHOT_MISSING`, …).

**Standard-library node** (separate repo/PR): `ReadArtifactProvenance` — input: artifact or path; outputs: producing node name, parameters (JSON), summary, chain (JSON). A thin wrapper over `Get` and `ResolveChain` for in-graph use.

## 10. Cloud backend

Today the Griptape Cloud storage driver skips all metadata — cloud artifacts have zero provenance. Under this design:

- `StaticFilesManager` (not the driver — drivers stay engine-agnostic) invokes capture after a successful upload.
- Records and pointers are uploaded **through the same driver** as bucket objects under the central prefix: artifact key `renders/hero.png` → record key `griptape-nodes-provenance/by-path/renders/hero.png/<record_id>.yaml`.
- `FAIL_ARTIFACT_SAVE` on cloud: record-upload failure triggers a best-effort delete of the just-uploaded artifact, then the failure surfaces. (Cloud has no atomic staging; last-writer-wins is the existing cloud posture and is documented.)
- **V1 limitation**: source discovery for cloud-held inputs requires a prefix listing the driver does not yet expose; v1 resolves parents via the local mirror when one exists and emits chain-break links otherwise. Prefix listing is a noted follow-up.

## 11. Subsuming the old sidecar system

- `OSManager` stops calling `write_sidecar`; the provenance hook replaces it at the same site (including the extension-swap variable fixup, moved verbatim into `ProvenanceContent.situation` handling).
- `WriteFileRequest.file_metadata` survives **one release** as a deprecated shim: when `provenance` is absent but `file_metadata` is supplied, OSManager synthesizes a `ProvenanceContent` from it with `WARN_AND_CONTINUE` (legacy callers must not start hard-failing) and logs a deprecation warning. The `File` API mirrors the old sidecar defaulting: a MacroPath-backed file with no explicit election elects capture with a minimal situation context; a plain-string path elects nothing.
- Callers converted in the implementing change: `File._build_file_metadata`, `ProjectFileDestination.from_situation`, `StaticFilesManager`, and the preview writer (which passes `provenance=None` — previews are derived caches and get no records).
- **Existing `.griptape-nodes-metadata` sidecars are never converted** — they lack hashes, IDs, and immutability semantics, and conversion would fabricate identity. The query API surfaces an old sidecar read-only under `legacy_sidecar` when an artifact has zero records, clearly second-class.
- The `SAVE_GRIPTAPE_NODES_METADATA` situation stays in the default template (the read fallback needs it; user overlays may reference it), marked deprecated in its description. Removal is a future major template change.

## 12. Edge cases

| Case                                                  | Behavior                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Extension coercion (sniff-and-swap rename)            | Capture runs on the final on-disk name; `extension_coerced_from` preserves the requested suffix.                                                                                                                                                                                                                                                                                            |
| CREATE_NEW collision walk (`hero.png` → `hero_1.png`) | Record keys on the walked final name; `requested_path` preserves intent. Cross-file iteration history is a query (`producing_node_name` filter), not a storage concern.                                                                                                                                                                                                                     |
| Same bytes re-saved                                   | New record (per-save-event is the contract); `by-hash/` groups them naturally.                                                                                                                                                                                                                                                                                                              |
| Artifact saved outside the workspace                  | Mirrors under `{drive_volume_mount}` — no collisions (retires the old sidecar defect). `workspace_relative_path: null`, `situation: null`.                                                                                                                                                                                                                                                  |
| Node renamed mid-session                              | Records are point-in-time facts and are never rewritten; queries by the old name find old records. A stable node UUID would improve this and is noted as future work.                                                                                                                                                                                                                       |
| Workflow never saved                                  | Capture proceeds with `workflow: null` (unlike the old sidecar, which bailed out entirely).                                                                                                                                                                                                                                                                                                 |
| Copy operations                                       | `CopyFileRequest` with `provenance` supplied writes a `relationship: "copied_from"` record for the destination with a single source link resolved from the source. `CopyTreeRequest` records nothing per-file in v1 (unbounded fan-out); note that copied *records* are not moved or duplicated — two paths claiming the same record identity would be two histories claiming one identity. |
| Shipped output trees                                  | Central store means records do not ride along in a zipped `renders/`; the project packager can include `griptape-nodes-provenance/` when provenance should ship with a delivery.                                                                                                                                                                                                            |
| Record/pointer writes themselves                      | Always `provenance=None` and `skip_metadata_injection=True` — no recursion, no records about records.                                                                                                                                                                                                                                                                                       |
| User deletes record files                             | Tolerated everywhere: missing records read as chain breaks; stale `by-hash/` pointers read as record-missing; nothing rebuilds or complains beyond the explicit break marker.                                                                                                                                                                                                               |

## 13. Out of scope / future work

- **C2PA / Content Credentials / signing** — deliberately deferred until the provenance *content* is right; nothing in this design blocks mapping the envelope onto C2PA assertions later.
- **Deprecating PNG `gtn_` auto-injection** (including the pickled `gtn_flow_commands` payload) — separate decision once this store ships; the workflow-from-image round trip would need a successor (e.g., reading the record store).
- **Indexes** — if scan performance at customer scale demands one, it must remain a derived, rebuildable file, never a source of truth.
- **Editor iteration view** ("session history"; terminology under review — see #5627) — a scoped, resettable view over the query API; reset is a client-side cursor and never touches canon.
- **Cloud prefix listing** for cloud-side source discovery.
- **Stable node UUIDs** to survive node renames in history queries.
- **In-memory provenance refs** to bridge chain breaks for artifacts that pass between nodes without a file write.
- **Path forms in the query API** (parked, design review 2026-09-23): write-side results use explicit absolute paths, but query results that cross machines (editor on a different mount, committed stores) likely want workspace-relative or macro forms. Decide when the API surface is specced.
- **Typed record IDs and content hashes**: harden the string fields with `Field(pattern=...)` constraints (which flow into the published JSON Schema) and `NewType` aliases; the record-ID shape is load-bearing (lexicographic = chronological, ID = filename) and deserves parse-time rejection of malformed values.
- **Provider-supplied destinations**: a `FileDestinationProvider` (e.g. FileOutputSettings) returns a pre-built destination, so those saves currently fall back to inferred node identity; stamping the saving node's identity onto provider-supplied destinations is a follow-up.

## 14. Test plan

- **Policy matrix**: capture policy × failure policy × write mode (OVERWRITE / FAIL / CREATE_NEW / append), asserting: record presence and shape, rollback on forced record failure (temp deleted, exclusive-create unlinked, append pre-flight), and `WARN_AND_CONTINUE` proceeding with a logged warning.
- **Layout**: collision-walk and extension-coercion saves land records under final names with `requested_path`/`extension_coerced_from` populated; outside-workspace saves mirror under `{drive_volume_mount}`; by-hash pointers resolve to canonical records.
- **Lineage fixtures**: generation (input → output source link), copy (`copied_from`), mutation (crop-and-resave), overwrite-same-file history, hash X/Y drift (`matches_latest_record: false`), renamed-input recovery via by-hash, chain break for never-saved inputs, cycle guard on a crafted loop.
- **Query API**: latest/specific/`include_payload` gets; node/workflow/time filters with the filename prefilter (assert out-of-range files are not opened, via instrumentation); descendants scan; legacy sidecar fallback.
- **Payload completeness**: a captured record's payload round-trips — commands present, pickled unique values unpickle to a non-empty dict, and (at FULL) the embedded workflow text carries the PEP-723 header and matches its hash.
- **Cloud**: fake driver asserting record/pointer keys under the central prefix and best-effort artifact delete on record failure.
- **Schema contract**: generated JSON Schema validates golden records; a `2.0.0` fixture surfaces "newer engine" instead of a parse error.
