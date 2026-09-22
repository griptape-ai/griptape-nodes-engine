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
1. Each record is **two-layer**: a human-readable JSON envelope plus a faithful payload produced by the engine's existing serialization machinery (many parameter types cannot be represented as plain text).
1. Parent links are **hybrid**: a precise reference `(path_at_use, record_id, content_hash)` plus an embedded one-line ancestor summary that survives even if the ancestor's record is later deleted.
1. Lineage attaches at **file boundaries**. The standard library passes media between nodes exclusively as file-backed URL artifacts, so this covers real flows; a third-party node that hands raw bytes downstream without a save creates a documented chain break.
1. **Out of scope**: C2PA/signing, databases or index files (plain files + scans only), and embedding anything in file-format metadata (EXIF/PNG chunks).
1. The new store **subsumes** the old sidecar system (§11).
1. Records are **dual-addressed**: canonical by path, with a lightweight by-hash pointer, so moved/renamed/hand-copied files regain their provenance via content hash.

## 3. Concepts and vocabulary

- **Record** — one immutable JSON file describing one save event of one artifact.
- **Record ID** — time-sortable unique identifier; also the record's filename.
- **Central store** — the visible `griptape-nodes-provenance/` directory holding all records, pointers, and snapshots for a project.
- **Snapshot** — a content-addressed serialized copy of the entire flow, shared by all records captured at `FULL_WORKFLOW_SNAPSHOT` during a run.
- **Parent link** — a record's reference to an input artifact's record: the lineage edge.
- **Chain break** — a parent link whose `record_id` is null because the input has no discoverable record (never saved through the engine, pre-feature file, or cross-project input).
- **Capture policy / failure policy** — the two per-save knobs (§6).
- **Canonical history vs. iteration view** — the record store is canonical and append-only; the artist-facing scoped view ("what have I made with this node since I switched tasks") is an editor concern built on the query API (#5627).

## 4. Record store layout

All provenance lives under one visible root, resolved through the project-template situation system:

```text
griptape-nodes-provenance/
├── by-path/                                  # canonical records, mirrored by source path
│   └── renders/
│       └── hero.png/                         # one directory per artifact file
│           ├── 20260921T182655120044Z-77be01c9.json
│           └── 20260921T183012482913Z-a3f9c2d1.json
├── by-hash/                                  # pointer files, addressed by content hash
│   └── 9f/
│       └── 9f2c…{full hash}…/
│           └── 20260921T183012482913Z-a3f9c2d1.json   # content: relative path to canonical record
└── snapshots/
    └── b17e4c20d3aa91f0.gtnflow              # content-addressed flow snapshot
```

### The situation macro

A new `BuiltInSituation.SAVE_GRIPTAPE_NODES_PROVENANCE` is added to `common/project_templates/situation.py` and declared in both the v0 and v1 default templates (mirroring how `SAVE_GRIPTAPE_NODES_PREVIEW` is declared):

```text
{griptape-nodes-provenance}/by-path/{drive_volume_mount?:/}{source_relative_path?:/}{source_file_name}/{provenance_record_id}.json
```

with `on_collision=FAIL` (record IDs never collide; a collision is a bug that must surface) and `create_dirs=True`. The `{griptape-nodes-provenance}` directory definition is `griptape-nodes-provenance` at the project/workspace root in v0 and `{workflow_dir?:/}griptape-nodes-provenance` in v1, matching the preview convention.

- **Outside-workspace files** mirror under the `{drive_volume_mount}` segment exactly as previews do (`C:\temp\x.png` → `by-path/C:/temp/x.png/…`), which is what the old sidecar macro lacked — its same-name collisions are retired by this design.
- **Resolve-time variables** include both the decomposed source variables *and* `artifact_dir` (the artifact's own parent directory), so a studio can flip the macro to an output-adjacent layout with a one-line template edit. Readers (parent discovery, queries) resolve record locations through the same situation machinery — always possible because a project context always exists. Overriding the macro to a shape readers cannot mirror is supported at-your-own-risk, the same posture as custom preview macros.

### Why centralized (and why visible)

Centralized: output/delivery directories stay clean; history queries glob one root instead of walking the workspace; the store is a single unit to commit, back up, or clear; snapshots dedupe project-wide; and previews already proved the mirroring scheme. The costs, accepted and documented: records do not travel inside a zipped output tree (the project packager can include the central store when provenance should ship), and inputs from *another* project's workspace resolve to the consuming project's store and appear as chain breaks.

Visible: customers will commit these files and build tooling against them; zip GUIs, sync tools, and artists routinely skip dot-directories. A side effect worth noting: the store shows up in generic file listings and is servable by the static server — which lets the editor fetch records over HTTP like any other workspace file.

### Dual addressing: by-path + by-hash

The canonical record lives in `by-path/`. Every record write also drops a tiny **pointer file** at `by-hash/{first two hex}/{full content hash}/{record_id}.json`, whose content is the relative path to the canonical record. Rationale:

- Path is where humans look, but it is the **least stable identity** — and out-of-project paths rot fastest. Content hash survives rename, move, and out-of-band (Finder) copies.
- On a path miss, lookups hash the file and consult `by-hash/` — moved, renamed, and hand-copied files regain their provenance.
- One hash can map to many records (same bytes saved repeatedly), hence a directory per hash; reusing the record ID as the pointer filename keeps hash-side listings time-sorted too.
- A pointer, not a duplicate record: full duplication would double every write and create two copies that can drift across schema migrations.

Pointer writes are covered by the same failure policy as the record. A missing pointer degrades to path-only lookup; a stale pointer (user deleted the canonical record) reads as record-missing — the same tolerance as chain breaks.

## 5. Record schema

### Record ID

```text
20260921T183012482913Z-a3f9c2d1
└──── UTC, µs precision ────┘ └ 8 hex chars of os.urandom ┘
```

Generatable at write time with zero coordination; lexicographic order equals chronological order, so "latest record" is the lexicographic max of a directory listing and no index is ever needed. The ID **is** the filename, so resolving a parent link is a direct file open, never a scan. (UUIDv7 was rejected: extra dependency, and the hand-rolled form is readable in `ls` output.)

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

  "parameters": { "prompt": "castle at dawn", "steps": 30 },
  "parameters_omitted": ["api_key_override"],       // exclude_from_metadata=True parameters, named but not valued

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

  "parents": [
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
    "kind": "node_commands_and_flow_snapshot",         // "none" | "node_commands" | "node_commands_and_flow_snapshot"
    "node_commands": { /* SerializeNodeToCommands result, inline JSON */ },
    "flow_snapshot_file": "snapshots/b17e4c20d3aa91f0.gtnflow",  // relative to the central store root
    "flow_snapshot_hash": "blake2b-256:b17e…",
    "flow_snapshot_format": "gtn-flow-commands-pickle-v1"
  }
}
```

Notes:

- **Content hashing is effectively free**: the write pipeline holds the full byte content in memory at capture time; `blake2b` over in-memory bytes costs microseconds-to-milliseconds. The hash covers the bytes *as written* (after any in-band metadata injection).
- **`summary_line`** is computed once, when the record is written. Children copy a parent's `summary_line` verbatim into their parent links — the hybrid summary costs O(1) per save instead of re-deriving ancestor descriptions.
- **Parameters** use the existing readable-summary path (`_collect_parameter_values` in `workflow_metadata.py`, `safe_unstructure`, honoring each parameter's `exclude_from_metadata` flag). Omitted parameters are *named* in `parameters_omitted` so redaction is visible, not silent.
- The **hash X/Y problem** is solved structurally: if file A (hash X) fed the generation of file B, and A is later re-saved with different content (hash Y), B's parent link still names A's *specific immutable record*. Drift is detectable — a reader comparing A's current bytes to the linked record's hash can flag "this input has changed since generation," which is a feature for artists, not just a failure mode.

### Payload: FULL is a strict superset

The envelope is identical at every capture level; only `payload` grows, so lowering a project's policy never changes record shape:

- **`PRODUCING_NODE_ONLY`** → `node_commands` inline: the producing node serialized via the existing `SerializeNodeToCommandsRequest` machinery. Single-node payloads are small in practice (media parameters are URL artifacts, so serialized values carry URLs, not pixels).
- **`FULL_WORKFLOW_SNAPSHOT`** → the inline `node_commands` **plus** a reference to a flow snapshot: `pickle.dumps(SerializedFlowCommands)` (the same machinery workflow save uses) written as a standalone content-addressed file under `snapshots/`. Snapshots are written with `on_collision=FAIL`, and an already-exists result is treated as success — content addressing means same name ⇒ same bytes. A run that saves ten artifacts serializes the flow once (per-run memo in the manager) and writes one snapshot; all ten records point at it.

### Schematization and the external contract

Pydantic models in a new `retained_mode/file_metadata/provenance_record.py`: `ProvenanceRecord`, `ArtifactIdentity`, `ProducingNodeIdentity`, `ParentLink`, `PayloadBlock`, `WorkflowIdentity`, `PackageIdentity`. Written with `model_dump_json`, validated on read with `model_validate`.

Versioning: readers accept any `1.x`. Additive fields bump the minor version; a breaking shape change bumps the major version and readers surface "this record was written by a newer engine" rather than guessing.

Because customers will commit these files and build pipelines against them, the record schema is a **supported external contract**: the generated JSON Schema (pydantic emits it) is published in the docs per `schema_version`, so third-party tooling can validate records without importing engine code.

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

Project defaults live on the **project template** (a new optional `ProvenanceSettings` block on `ProjectTemplate`: `default_capture_policy`, `default_failure_policy`), not on a config key — policy must travel with projects and published packages, and the template overlay/merge machinery already exists.

Resolution ladder: request value → (if inherit) project template block → (if absent) engine constants (`FULL_WORKFLOW_SNAPSHOT`, `FAIL_ARTIFACT_SAVE`).

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

`WriteFileRequest` and `CopyFileRequest` gain `provenance: ProvenanceContent | None = None`. **`None` means no capture at all**, mirroring today's `file_metadata` opt-in. This one rule keeps internal writes — previews, temp files, and the record/pointer/snapshot writes themselves — out of the system with no recursion guard.

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

### Concurrency

A per-record-directory `KeyedMutex` (the preview system's pattern), keyed on `canonicalize_for_identity` of the record directory, protects the read-latest-parent/write-record window. Record files themselves never contend — their names are unique.

## 8. Parent discovery

At capture time, given the producing node:

1. **Enumerate** parameter values for parameters whose `allowed_modes` include INPUT or PROPERTY (the same filter the readable-parameter summary uses); flatten one level of lists/dicts.
1. **Classify file-backed values**, first match wins:
    - URL artifacts (`ImageUrlArtifact` etc.): static-server URLs map back to workspace paths; cloud asset URLs map via the cloud driver's URL-to-path extraction.
    - `str` values that resolve to an existing file **and** carry an extension known to the `ProviderRegistry` (both gates required, so prose that merely mentions a filename is not misread as lineage).
    - `File`/`MacroPath`-bearing objects resolve directly.
1. **Resolve** each input path to its record directory through the same situation machinery writers use.
1. **Latest record** = lexicographic max of the directory. Extract `record_id`, `content_hash`, `size_bytes`, `summary_line`.
1. **Staleness**: compare the input file's current size to the record's; only if sizes match, hash the bytes. `matches_latest_record: false` means the input changed after its last record. The parent link's `content_hash` is always the input's bytes *as consumed now*.
1. **No records at the path** → hash the input and try `by-hash/` before declaring a break — this recovers renamed and hand-copied inputs. Still nothing → emit the link with `record_id: null` (path and hash still recorded): the documented chain break.

Any single parent's discovery error (unreadable JSON, stat failure) degrades to a `record_id: null` link with an artist-readable `discovery_error` field. Parent discovery never fails the save on its own — only record *writing* participates in the failure policy.

## 9. Query API

A new `ProvenanceManager` (extends `EngineScoped`, constructed by `Engine` like its ~28 peers; folding into the already-large `ArtifactManager` was rejected) handles a new event family in `retained_mode/events/provenance_events.py`:

| Request                                                                                                                                      | Answers                                                                  | Mechanics                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GetProvenanceForArtifactRequest(file_path, record_id=None, include_payload=False)`                                                          | "What made this file?"                                                   | Latest (or specific) record for the path; `is_stale` compares current bytes to the record hash; **hash fallback** on a path miss; `legacy_sidecar` fallback (§11).          |
| `ListProvenanceRecordsForArtifactRequest(file_path)`                                                                                         | "Every iteration of this file"                                           | Directory listing, newest first — the overwrite-same-file history.                                                                                                          |
| `ListProvenanceRecordsForHashRequest(content_hash)`                                                                                          | "Every record for these exact bytes"                                     | Direct `by-hash/` lookup, for tooling that already has the hash.                                                                                                            |
| `ListProvenanceRecordsRequest(producing_node_name=None, node_type=None, workflow_name=None, saved_after=None, saved_before=None, limit=200)` | The artist's iteration query ("everything TextToImage_1 made this week") | Glob of the single central root — no workspace walk. Record-ID filenames double as a **time-range prefilter**: files outside `saved_after`/`saved_before` are never opened. |
| `ResolveProvenanceChainRequest(file_path, record_id=None, max_depth=16)`                                                                     | "The whole ancestry"                                                     | Recurses parent links; each hop is a direct file open (the ID is the filename). Visited-set cycle guard; breaks surface as explicit nodes.                                  |
| `ListDescendantsForArtifactRequest(file_path, record_id=None)`                                                                               | Reverse lineage: "which artifacts used this one?"                        | Central-root scan matching parent links by path/record ID/hash — the impact question artists and studios both ask.                                                          |

Scans are O(records) by design (no index); the central root and filename prefilter keep them cheap for editor-driven browsing. Failure reasons are a `ProvenanceFailureReason` StrEnum (`ARTIFACT_NOT_FOUND`, `NO_PROVENANCE_RECORDS`, `RECORD_UNREADABLE`, `SNAPSHOT_MISSING`, …).

**Standard-library node** (separate repo/PR): `ReadArtifactProvenance` — input: artifact or path; outputs: producing node name, parameters (JSON), summary, chain (JSON). A thin wrapper over `Get` and `ResolveChain` for in-graph use.

## 10. Cloud backend

Today the Griptape Cloud storage driver skips all metadata — cloud artifacts have zero provenance. Under this design:

- `StaticFilesManager` (not the driver — drivers stay engine-agnostic) invokes capture after a successful upload.
- Records and snapshots are uploaded **through the same driver** as bucket objects under the central prefix: artifact key `renders/hero.png` → record key `griptape-nodes-provenance/by-path/renders/hero.png/<record_id>.json`, snapshots under `griptape-nodes-provenance/snapshots/`.
- `FAIL_ARTIFACT_SAVE` on cloud: record-upload failure triggers a best-effort delete of the just-uploaded artifact, then the failure surfaces. (Cloud has no atomic staging; last-writer-wins is the existing cloud posture and is documented.)
- **V1 limitation**: parent discovery for cloud-held inputs requires a prefix listing the driver does not yet expose; v1 resolves parents via the local mirror when one exists and emits chain-break links otherwise. Prefix listing is a noted follow-up.

## 11. Subsuming the old sidecar system

- `OSManager` stops calling `write_sidecar`; the provenance hook replaces it at the same site (including the extension-swap variable fixup, moved verbatim into `ProvenanceContent.situation` handling).
- `WriteFileRequest.file_metadata` survives **one release** as a deprecated shim: when `provenance` is absent but `file_metadata` is supplied, OSManager synthesizes a `ProvenanceContent` from it with `WARN_AND_CONTINUE` (legacy callers must not start hard-failing) and logs a deprecation warning.
- Callers converted in the implementing change: `File._build_file_metadata`, `ProjectFileDestination.from_situation`, `StaticFilesManager`, and the preview writer (which passes `provenance=None` — previews are derived caches and get no records).
- **Existing `.griptape-nodes-metadata` sidecars are never converted** — they lack hashes, IDs, and immutability semantics, and conversion would fabricate identity. The query API surfaces an old sidecar read-only under `legacy_sidecar` when an artifact has zero records, clearly second-class.
- The `SAVE_GRIPTAPE_NODES_METADATA` situation stays in the default template (the read fallback needs it; user overlays may reference it), marked deprecated in its description. Removal is a future major template change.

## 12. Edge cases

| Case                                                  | Behavior                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Extension coercion (sniff-and-swap rename)            | Capture runs on the final on-disk name; `extension_coerced_from` preserves the requested suffix.                                                                                                                                                                                                                                                                                            |
| CREATE_NEW collision walk (`hero.png` → `hero_1.png`) | Record keys on the walked final name; `requested_path` preserves intent. Cross-file iteration history is a query (`producing_node_name` filter), not a storage concern.                                                                                                                                                                                                                     |
| Same bytes re-saved                                   | New record (per-save-event is the contract); the snapshot store and `by-hash/` dedupe naturally.                                                                                                                                                                                                                                                                                            |
| Artifact saved outside the workspace                  | Mirrors under `{drive_volume_mount}` — no collisions (retires the old sidecar defect). `workspace_relative_path: null`, `situation: null`.                                                                                                                                                                                                                                                  |
| Node renamed mid-session                              | Records are point-in-time facts and are never rewritten; queries by the old name find old records. A stable node UUID would improve this and is noted as future work.                                                                                                                                                                                                                       |
| Workflow never saved                                  | Capture proceeds with `workflow: null` (unlike the old sidecar, which bailed out entirely).                                                                                                                                                                                                                                                                                                 |
| Copy operations                                       | `CopyFileRequest` with `provenance` supplied writes a `relationship: "copied_from"` record for the destination with a single parent link resolved from the source. `CopyTreeRequest` records nothing per-file in v1 (unbounded fan-out); note that copied *records* are not moved or duplicated — two paths claiming the same record identity would be two histories claiming one identity. |
| Shipped output trees                                  | Central store means records do not ride along in a zipped `renders/`; the project packager can include `griptape-nodes-provenance/` when provenance should ship with a delivery.                                                                                                                                                                                                            |
| Record/pointer/snapshot writes themselves             | Always `provenance=None` and `skip_metadata_injection=True` — no recursion, no records about records.                                                                                                                                                                                                                                                                                       |
| User deletes record files                             | Tolerated everywhere: missing records read as chain breaks; stale `by-hash/` pointers read as record-missing; nothing rebuilds or complains beyond the explicit break marker.                                                                                                                                                                                                               |

## 13. Out of scope / future work

- **C2PA / Content Credentials / signing** — deliberately deferred until the provenance *content* is right; nothing in this design blocks mapping the envelope onto C2PA assertions later.
- **Deprecating PNG `gtn_` auto-injection** (including the pickled `gtn_flow_commands` payload) — separate decision once this store ships; the workflow-from-image round trip would need a successor (e.g., reading the record store).
- **Indexes** — if scan performance at customer scale demands one, it must remain a derived, rebuildable file, never a source of truth.
- **Editor iteration view** ("session history"; terminology under review — see #5627) — a scoped, resettable view over the query API; reset is a client-side cursor and never touches canon.
- **Cloud prefix listing** for cloud-side parent discovery.
- **Stable node UUIDs** to survive node renames in history queries.
- **In-memory provenance refs** to bridge chain breaks for artifacts that pass between nodes without a file write.

## 14. Test plan

- **Policy matrix**: capture policy × failure policy × write mode (OVERWRITE / FAIL / CREATE_NEW / append), asserting: record presence and shape, rollback on forced record failure (temp deleted, exclusive-create unlinked, append pre-flight), and `WARN_AND_CONTINUE` proceeding with a logged warning.
- **Layout**: collision-walk and extension-coercion saves land records under final names with `requested_path`/`extension_coerced_from` populated; outside-workspace saves mirror under `{drive_volume_mount}`; by-hash pointers resolve to canonical records.
- **Lineage fixtures**: generation (input → output parent link), copy (`copied_from`), mutation (crop-and-resave), overwrite-same-file history, hash X/Y drift (`matches_latest_record: false`), renamed-input recovery via by-hash, chain break for never-saved inputs, cycle guard on a crafted loop.
- **Query API**: latest/specific/`include_payload` gets; node/workflow/time filters with the filename prefilter (assert out-of-range files are not opened, via instrumentation); descendants scan; legacy sidecar fallback.
- **Snapshot dedupe**: a run saving N artifacts writes one snapshot; already-exists treated as success.
- **Cloud**: fake driver asserting record/snapshot keys under the central prefix and best-effort artifact delete on record failure.
- **Schema contract**: generated JSON Schema validates golden records; a `2.0.0` fixture surfaces "newer engine" instead of a parse error.
