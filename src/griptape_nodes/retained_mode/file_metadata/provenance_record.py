"""Provenance record schema: the immutable per-save-event provenance document.

One record is written per elected save event, never overwritten. Canonical
records live in the central store's `by-path/` mirror; each record write also
drops a pointer file under `by-hash/` so content that moves or is copied
outside the engine can be re-associated with its history. Records are YAML
with in-band comments so a future inspector (human or agent) can interpret
them cold; at FULL_WORKFLOW_SNAPSHOT each record embeds the entire workflow as
runnable .py file text.

The record envelope is a supported external contract: customers commit these
files and build tooling against them. Readers accept any 1.x schema version;
additive fields bump the minor version, breaking changes bump the major.

Design: docs/development/designs/artifact_provenance.md (§4, §5).
"""

from __future__ import annotations

import hashlib
import io
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import semver
from pydantic import BaseModel, Field
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from griptape_nodes.common.project_templates.provenance_settings import (
    ProvenanceCapturePolicy,
    ProvenanceFailurePolicy,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SituationMetadata

RECORD_SCHEMA_VERSION = "1.0.0"

# Layout contract inside the central provenance store. Writers resolve the
# by-path location through the SAVE_ARTIFACT_PROVENANCE situation macro;
# these names are the pieces readers and derived paths (by-hash pointers,
# snapshots) build from.
BY_PATH_DIR_NAME = "by-path"
BY_HASH_DIR_NAME = "by-hash"

CONTENT_HASH_ALGORITHM = "blake2b-256"
_CONTENT_HASH_DIGEST_SIZE = 32
# Fan-out sharding for by-hash/: first N hex chars of the digest become an
# intermediate directory so one directory never accumulates every hash.
BY_HASH_SHARD_CHARS = 2

# Format tag for the pickled unique-parameter-values blob inside a record's
# payload: base64 of pickle.dumps(unique_parameter_uuid_to_values), exactly the
# dict the engine's node serialization protocol fills in.
PARAMETER_VALUES_FORMAT = "gtn-unique-values-pickle-v1"

RECORD_FILE_EXTENSION = "yaml"

_RECORD_ID_RANDOM_BYTES = 4
_RECORD_ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%f"


class ProvenanceRelationship(StrEnum):
    """How the artifact came to exist, from the record's point of view."""

    PRODUCED = "produced"
    COPIED_FROM = "copied_from"


class ProducingNodeIdentitySource(StrEnum):
    """Whether the producing node was attested by the caller or inferred from flow state."""

    EXPLICIT = "explicit"
    INFERRED_RESOLVING_NODE = "inferred_resolving_node"


class ProducingNodeIdentity(BaseModel):
    """Identity of the node that performed the save."""

    name: str
    node_type: str
    library_name: str | None = None
    library_version: str | None = None
    identity_source: ProducingNodeIdentitySource = Field(
        default=ProducingNodeIdentitySource.EXPLICIT,
        description=(
            "'explicit' when the saving code attested its own node identity (trustworthy). "
            "'inferred_resolving_node' when the save carried no identity (e.g. a library "
            "predating provenance) and the engine attributed it to whichever node was "
            "executing at the time -- a guess that can name the wrong node when several "
            "nodes run in parallel."
        ),
    )


class ArtifactIdentity(BaseModel):
    """Identity of the saved artifact: the final on-disk truth for this save event."""

    path_at_save: str = Field(
        description=(
            "Absolute path the bytes landed at on the capturing machine (post collision-walk "
            "and extension coercion). Machine-local truth as of this save; macro_path is the "
            "portable identity."
        )
    )
    macro_path: str | None = Field(
        default=None,
        description=(
            "Portable macro form of path_at_save (e.g. '{outputs}/hero.png') when the path maps "
            "into a project directory; re-resolves against the current project on any machine. "
            "Null when the location has no macro mapping."
        ),
    )
    file_name: str
    content_hash: str = Field(description="Hash of the exact bytes written, e.g. 'blake2b-256:<hex>'")
    size_bytes: int
    append: bool = Field(
        default=False,
        description=(
            "True when this save APPENDED to an existing file (log-style writes) rather than "
            "replacing it; content_hash then covers the whole resulting file, not just the "
            "bytes this save added. Media saves are never appends."
        ),
    )
    extension_coerced_from: str | None = Field(
        default=None, description="Requested extension when sniff-and-swap renamed the file"
    )


class WorkflowIdentity(BaseModel):
    """Workflow context at save time; a record with none means the workflow was never saved."""

    name: str | None = None
    flow_name: str | None = None
    created: str | None = None
    modified: str | None = None
    engine_version_created_with: str | None = None


class PackageIdentity(BaseModel):
    """Published-package context, when the running project came from a package."""

    source_project_id: str | None = None
    package_project_name: str | None = None
    exported_at: str | None = None


class SourceLink(BaseModel):
    """Lineage edge to one source artifact, hybrid form: precise reference plus summary.

    A null record_id is a chain break: the source has no discoverable record
    (never saved through the engine, pre-feature file, or cross-project input).
    The path and hash are still recorded so the break is a fact, not a blank.
    """

    path_at_use: str = Field(description="Absolute path the source was consumed from, on the capturing machine")
    macro_path: str | None = Field(
        default=None,
        description=(
            "Portable macro form of path_at_use when it maps into a project directory; "
            "null for locations outside every project directory"
        ),
    )
    parameter_name: str | None = None
    record_id: str | None = None
    content_hash: str | None = Field(default=None, description="Hash of the source's bytes as consumed")
    matches_latest_record: bool | None = Field(
        default=None, description="False when the source changed after its latest record; null on chain break"
    )
    summary: str | None = Field(default=None, description="The source record's summary_line, copied verbatim")
    discovery_error: str | None = Field(
        default=None, description="Artist-readable cause when discovery degraded for this source"
    )


class SerializedNodePayload(BaseModel):
    """The producing node via the FULL serialize-node protocol: rebuildable by an engine.

    PERSISTENCE COUPLING: the dict fields hold the event-converter unstructured
    forms of SerializedNodeCommands and its IndirectSetParameterValueCommand
    list (node_events.py). Changes to those dataclasses must keep old records
    structurable, or bump RECORD_SCHEMA_VERSION -- there is a round-trip
    regression test that fails when the shapes drift apart.
    """

    serialized_node_commands: dict[str, Any] = Field(
        description=(
            "READABLE projection of SerializedNodeCommands (node type, parameter names, "
            "structure). Not faithfully restructurable: element commands are polymorphic; "
            "rehydrate from pickled_node_commands instead."
        )
    )
    pickled_node_commands: str | None = Field(
        default=None,
        description=(
            "base64(pickle.dumps(SerializedNodeCommands)): the FAITHFUL form for rehydration, "
            "the same transport the engine's clipboard path uses. Optional only for records "
            "written before this field existed."
        ),
    )
    set_parameter_value_commands: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Indirect set-value commands keying into the pickled unique-values dict",
    )
    pickled_parameter_values: str = Field(
        description="base64(pickle.dumps(unique_parameter_uuid_to_values)); engine-only, see parameter_values_format",
    )
    parameter_values_format: str = Field(
        description=f"Format tag for pickled_parameter_values; currently '{PARAMETER_VALUES_FORMAT}'"
    )


class SerializedWorkflowPayload(BaseModel):
    """The entire workflow embedded as a runnable .py workflow file's text."""

    workflow_file_content: str = Field(
        description="Full text of a runnable workflow .py file capturing the whole flow at save time",
    )
    workflow_file_hash: str = Field(description="Content hash of workflow_file_content for integrity checks")


class ProvenancePayload(BaseModel):
    """Faithful payload block: the record's engine-deserializable truth.

    Presence of a sub-object IS the answer to "do you have it": records at
    PRODUCING_NODE_ONLY carry serialized_node; FULL_WORKFLOW_SNAPSHOT adds
    serialized_workflow. An engine rebuilds the node from serialized_node with
    the same machinery paste/workflow-load uses; serialized_workflow's text
    extracts to disk as a loadable .py (the engine's versioned,
    migration-supported container).
    """

    serialized_node: SerializedNodePayload | None = None
    serialized_workflow: SerializedWorkflowPayload | None = None


class _RecordEnvelopeFields(BaseModel):
    """Shared envelope fields: everything about a save event except the payload."""

    schema_version: str = RECORD_SCHEMA_VERSION
    record_id: str
    saved_at: str = Field(description="UTC ISO-8601 timestamp of the save event")
    capture_policy: ProvenanceCapturePolicy = Field(
        description="The RESOLVED capture policy; never inherit_project_policy"
    )
    relationship: ProvenanceRelationship = ProvenanceRelationship.PRODUCED
    engine_version: str | None = None
    summary_line: str | None = Field(
        default=None, description="One-line human summary; children copy it into their parent links verbatim"
    )
    artifact: ArtifactIdentity
    producing_node: ProducingNodeIdentity | None = None
    parameters_preview: dict[str, Any] | None = Field(
        default=None,
        description=(
            "LOSSY, display-only projection of the producing node's parameter values. "
            "The payload block is the only faithful representation; never rehydrate from this."
        ),
    )
    parameters_omitted: list[str] | None = Field(
        default=None, description="Names of exclude_from_metadata parameters: redaction is visible, not silent"
    )
    workflow: WorkflowIdentity | None = None
    package: PackageIdentity | None = None
    situation: SituationMetadata | None = None
    sources: list[SourceLink] = Field(default_factory=list)


class ProvenanceRecord(_RecordEnvelopeFields):
    """The immutable per-save-event provenance document, as stored on disk.

    One self-contained file: the envelope plus the full faithful payload.
    This shape never goes over the wire wholesale -- payloads embed entire
    workflows and run to hundreds of KB. Query results carry
    ProvenanceRecordHeader instead.
    """

    payload: ProvenancePayload = Field(default_factory=ProvenancePayload)


class ProvenancePayloadSummary(BaseModel):
    """What a record's payload holds, without holding it.

    Enough for a caller to decide whether fetching the full payload
    (GetProvenancePayloadRequest) is worth the weight.
    """

    has_serialized_node: bool = False
    has_serialized_workflow: bool = False
    pickled_parameter_values_bytes: int | None = Field(
        default=None, description="Encoded size of the pickled unique-values blob"
    )
    workflow_file_chars: int | None = Field(default=None, description="Length of the embedded workflow .py text")
    workflow_file_hash: str | None = None

    @classmethod
    def from_payload(cls, payload: ProvenancePayload) -> ProvenancePayloadSummary:
        """Summarize a full payload for the wire."""
        summary = cls()
        if payload.serialized_node is not None:
            summary.has_serialized_node = True
            summary.pickled_parameter_values_bytes = len(payload.serialized_node.pickled_parameter_values)
        if payload.serialized_workflow is not None:
            summary.has_serialized_workflow = True
            summary.workflow_file_chars = len(payload.serialized_workflow.workflow_file_content)
            summary.workflow_file_hash = payload.serialized_workflow.workflow_file_hash
        return summary


class ProvenanceRecordHeader(_RecordEnvelopeFields):
    """The wire shape of a record: the full envelope, with the payload summarized.

    Same fields as the on-disk record except `payload` carries a
    ProvenancePayloadSummary instead of the (potentially huge) payload itself.
    """

    payload: ProvenancePayloadSummary = Field(default_factory=ProvenancePayloadSummary)

    @classmethod
    def from_record(cls, record: ProvenanceRecord) -> ProvenanceRecordHeader:
        """Build the wire header from an on-disk record."""
        envelope = record.model_dump(exclude={"payload"})
        return cls(**envelope, payload=ProvenancePayloadSummary.from_payload(record.payload))


class ByHashPointer(BaseModel):
    """Content of a by-hash pointer file: where the canonical record lives.

    The record path is relative to the central provenance store root so the
    pointer survives the store being moved or committed to another machine;
    the artifact descriptor uses the portable macro form when one exists.
    """

    schema_version: str = RECORD_SCHEMA_VERSION
    record: str = Field(description="Store-root-relative path to the canonical record, POSIX separators")
    artifact_path: str = Field(
        description="The artifact this hash belonged to: macro form when mappable, absolute path otherwise"
    )


class ProvenanceContent(BaseModel):
    """Caller-supplied provenance election riding a write/copy request.

    None on the request means no capture at all; this is what keeps internal
    writes (previews, temp files, and the record/pointer/snapshot writes
    themselves) out of the provenance system with no recursion guard.
    """

    # None is the idiomatic request-side spelling of "inherit the project
    # policy" (INHERIT_PROJECT_POLICY remains a valid explicit spelling).
    capture_policy: ProvenanceCapturePolicy | None = None
    failure_policy: ProvenanceFailurePolicy | None = None
    producing_node: ProducingNodeIdentity | None = Field(
        default=None, description="None means the engine infers from flow state and flags the record as inferred"
    )
    situation: SituationMetadata | None = None


class ProvenanceWriteDetails(BaseModel):
    """Capture outcome returned on write/copy results, so callers never re-query for a record just created."""

    record_id: str
    record_path: str = Field(description="Resolved canonical record path (or cloud key)")
    content_hash: str
    capture_policy: ProvenanceCapturePolicy = Field(description="The RESOLVED policy; never inherit_project_policy")
    warning: str | None = Field(
        default=None, description="Set when warn_and_continue swallowed a record failure; the save still succeeded"
    )


def generate_record_id(now: datetime | None = None) -> str:
    """Generate a record ID: UTC microsecond timestamp + 8 hex chars of randomness.

    Lexicographic order equals chronological order (fixed-width timestamp
    prefix), so the latest record in a directory is the lexicographic max and
    no index is needed. The ID doubles as the record's filename stem.
    """
    if now is None:
        now = datetime.now(UTC)
    timestamp = now.strftime(_RECORD_ID_TIMESTAMP_FORMAT)
    random_suffix = os.urandom(_RECORD_ID_RANDOM_BYTES).hex()
    return f"{timestamp}Z-{random_suffix}"


def hash_content(data: bytes) -> str:
    """Hash artifact bytes into the record's content-hash form ('blake2b-256:<hex>')."""
    digest = hashlib.blake2b(data, digest_size=_CONTENT_HASH_DIGEST_SIZE).hexdigest()
    return f"{CONTENT_HASH_ALGORITHM}:{digest}"


def hash_hex_from_content_hash(content_hash: str) -> str:
    """Extract the bare hex digest from a 'algorithm:<hex>' content-hash string.

    Raises:
        ValueError: If the value does not carry the expected algorithm prefix.
    """
    prefix = f"{CONTENT_HASH_ALGORITHM}:"
    if not content_hash.startswith(prefix):
        msg = f"Content hash '{content_hash}' does not use the expected '{prefix}' form"
        raise ValueError(msg)
    return content_hash.removeprefix(prefix)


def by_hash_relative_path(content_hash: str, record_id: str) -> str:
    """Build the store-root-relative POSIX path of a by-hash pointer file.

    Layout: by-hash/<first BY_HASH_SHARD_CHARS hex>/<full hex>/<record_id>.yaml.
    Reusing the record ID as the pointer filename keeps hash-side listings
    time-sorted like the canonical side.
    """
    hex_digest = hash_hex_from_content_hash(content_hash)
    shard = hex_digest[:BY_HASH_SHARD_CHARS]
    return f"{BY_HASH_DIR_NAME}/{shard}/{hex_digest}/{record_id}.{RECORD_FILE_EXTENSION}"


def is_reader_compatible(record_schema_version: str) -> bool:
    """Whether this engine can read a record with the given schema version.

    Readers accept any version sharing RECORD_SCHEMA_VERSION's major. A newer
    major must surface as "record from a newer engine", never as a guess; a
    malformed version is treated as unreadable rather than raising on a read
    path fed by user-editable files.
    """
    try:
        record_version = semver.VersionInfo.parse(record_schema_version)
        reader_version = semver.VersionInfo.parse(RECORD_SCHEMA_VERSION)
    except ValueError:
        return False
    return record_version.major == reader_version.major


# In-band guidance for a future inspector (human or agent) opening a record
# cold. Static per schema version; injected as YAML comments on every record.
_RECORD_HEADER_COMMENT = f"""Griptape Nodes artifact provenance record (schema {RECORD_SCHEMA_VERSION}).
One immutable record per save event; never edited or overwritten.
The 'payload' block is the ONLY faithful representation of node/workflow state.
Everything else is identity, lineage, and display-level context."""

_RECORD_KEY_COMMENTS: dict[str, str] = {
    "artifact": "The saved file's identity: final on-disk truth for this save event.",
    "producing_node": "Which node performed the save; identity_source says attested vs inferred.",
    "parameters_preview": (
        "LOSSY display-only projection of parameter values. Do not rehydrate from this;\n"
        "the payload block is the truth. exclude_from_metadata parameters are omitted\n"
        "here (and named in parameters_omitted) but present in the payload."
    ),
    "workflow": "Workflow context at save time; absent means the workflow was never saved.",
    "sources": (
        "Lineage: one entry per artifact this save was made from. record_id null =\n"
        "chain break (the source has no record). content_hash is the source AS CONSUMED."
    ),
    "payload": "Engine-deserializable truth; each field explains itself below.",
}

# Per-section guidance inside the payload block, placed adjacent to the
# sub-object it explains (the block can be hundreds of KB long).
_PAYLOAD_KEY_COMMENTS: dict[str, str] = {
    "serialized_node": (
        "The producing node, rebuildable by an engine. serialized_node_commands is the\n"
        "READABLE projection (node type, parameter names, structure); rehydration uses\n"
        "pickled_node_commands + set-value commands + pickled_parameter_values (base64;\n"
        "engine-only) -- the same transport the clipboard path uses."
    ),
    "serialized_workflow": (
        "The ENTIRE workflow as a runnable .py workflow file's text.\n"
        "Extract workflow_file_content to disk and any Griptape Nodes engine loads it."
    ),
}

_POINTER_HEADER_COMMENT = """Griptape Nodes provenance by-hash pointer.
Maps this content hash to its canonical record under by-path/.
'record' is relative to the provenance store root."""


def dump_record_yaml(record: ProvenanceRecord) -> str:
    """Serialize a record to commented YAML: the on-disk record format."""
    plain = record.model_dump(mode="json", exclude_none=True)

    payload = plain.get("payload")
    if payload is not None:
        serialized_workflow = payload.get("serialized_workflow")
        if serialized_workflow is not None and serialized_workflow.get("workflow_file_content") is not None:
            # Literal block scalar: the embedded workflow .py stays readable
            # line-by-line instead of becoming one escape-laden quoted string.
            serialized_workflow["workflow_file_content"] = LiteralScalarString(
                serialized_workflow["workflow_file_content"]
            )
        # Guidance sits ADJACENT to the field it explains -- a payload block can
        # be hundreds of KB, so a comment at its top would be a scroll away.
        payload_map = CommentedMap(payload)
        for key, comment in _PAYLOAD_KEY_COMMENTS.items():
            if key in payload_map:
                payload_map.yaml_set_comment_before_after_key(key, before=comment)
        plain["payload"] = payload_map

    data = CommentedMap(plain)
    for key, comment in _RECORD_KEY_COMMENTS.items():
        if key in data:
            data.yaml_set_comment_before_after_key(key, before="\n" + comment)
    return _dump_commented(data, _RECORD_HEADER_COMMENT)


def load_record_yaml(text: str) -> ProvenanceRecord:
    """Parse a record from its on-disk YAML form.

    Raises:
        ValueError: If the text is not valid YAML or not a valid record.
    """
    try:
        raw = YAML(typ="safe").load(text)
    except Exception as e:
        msg = f"Not valid YAML: {e}"
        raise ValueError(msg) from e
    return ProvenanceRecord.model_validate(raw)


def dump_pointer_yaml(pointer: ByHashPointer) -> str:
    """Serialize a by-hash pointer to commented YAML."""
    data = CommentedMap(pointer.model_dump(mode="json", exclude_none=True))
    return _dump_commented(data, _POINTER_HEADER_COMMENT)


def load_pointer_yaml(text: str) -> ByHashPointer:
    """Parse a by-hash pointer from its on-disk YAML form.

    Raises:
        ValueError: If the text is not valid YAML or not a valid pointer.
    """
    try:
        raw = YAML(typ="safe").load(text)
    except Exception as e:
        msg = f"Not valid YAML: {e}"
        raise ValueError(msg) from e
    return ByHashPointer.model_validate(raw)


def _build_yaml() -> YAML:
    """YAML dumper with the provenance-store conventions.

    Mirrors the project-template conventions (build_project_yaml): every plain
    string double-quoted so YAML 1.1 coercions (the Norway problem) can never
    bite, wide lines, block style. Literal block scalars (the embedded workflow
    file) keep their own representer.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.representer.add_representer(str, lambda r, d: r.represent_scalar("tag:yaml.org,2002:str", d, style='"'))
    return yaml


def _dump_commented(data: CommentedMap, header_comment: str) -> str:
    data.yaml_set_start_comment(header_comment)
    stream = io.StringIO()
    _build_yaml().dump(data, stream)
    return stream.getvalue()
