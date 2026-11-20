"""Events for querying the artifact provenance store.

The request family is the ONLY interface to provenance for anything outside
the engine (editor, node libraries, external tooling); the ProvenanceManager's
public methods are the engine-internal capture protocol for OSManager.

Path inputs are named `macro_path` and typed `str | MacroPath` like the rest
of the OS request surface: pass a MacroPath for macro form, or a string for a
workspace-relative/absolute path (macro-shaped strings are upgraded the same
way File() upgrades them at its boundary).

Design: docs/development/designs/artifact_provenance.md (§9).
"""

from dataclasses import dataclass, field
from enum import StrEnum

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.file_metadata.provenance_record import (
    ProvenancePayload,
    ProvenanceRecordHeader,
)


class ProvenanceMatchOrigin(StrEnum):
    """How an artifact lookup found its records."""

    # The record directory for the queried path held the records.
    PATH = "path"
    # The path had no records; the file's content hash led to them (the file
    # was renamed or copied outside the engine since capture).
    HASH = "hash"


class ProvenanceQueryFailureReason(StrEnum):
    """Classification of provenance query failure reasons."""

    # The macro_path input could not be resolved to a location at all.
    PATH_UNRESOLVABLE = "path_unresolvable"
    # The location resolved, but no readable records exist for it (including
    # the by-hash fallback for moved/renamed files).
    NO_PROVENANCE_RECORDS = "no_provenance_records"
    # A specific record_id was requested and is not present (or unreadable).
    RECORD_NOT_FOUND = "record_not_found"
    # The provenance store itself could not be located (no project context).
    STORE_UNAVAILABLE = "store_unavailable"


@dataclass
@PayloadRegistry.register
class GetProvenanceForArtifactRequest(RequestPayload):
    """Get the latest (or a specific) provenance record for an artifact.

    Use when: answering "what made this file, from what, with what settings".

    Args:
        macro_path: The artifact's location: a MacroPath, or a string
            (workspace-relative, absolute, or macro-shaped).
        record_id: A specific record to fetch; None fetches the latest.

    On a path miss the lookup falls back to content-hash matching, so a file
    renamed or copied outside the engine still finds its history. There is no
    fallback to the legacy sidecar system (an evolutionary dead-end).

    Results: GetProvenanceForArtifactResultSuccess | GetProvenanceForArtifactResultFailure
    """

    macro_path: str | MacroPath
    record_id: str | None = None


@dataclass
@PayloadRegistry.register
class GetProvenanceForArtifactResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """A provenance record was found.

    Attributes:
        record: The record's wire header: the full envelope with the payload
            SUMMARIZED (payloads embed entire workflows; fetch one explicitly
            with GetProvenancePayloadRequest when needed).
        record_path: Absolute path of the record YAML on disk.
        is_stale: True when the artifact's live bytes no longer match the
            record's content hash; None when the live file is missing or
            unreadable (staleness cannot be judged).
    """

    record: ProvenanceRecordHeader
    record_path: str
    matched_by: ProvenanceMatchOrigin = ProvenanceMatchOrigin.PATH
    is_stale: bool | None = None


@dataclass
@PayloadRegistry.register
class GetProvenanceForArtifactResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """No provenance record could be produced for the artifact."""

    failure_reason: ProvenanceQueryFailureReason


@dataclass
@PayloadRegistry.register
class ListProvenanceRecordsForArtifactRequest(RequestPayload):
    """List one artifact's full save history, newest first.

    Use when: browsing iterations of a single file (including every overwrite
    of the same path — each save is its own immutable record).

    Args:
        macro_path: The artifact's location: a MacroPath, or a string
            (workspace-relative, absolute, or macro-shaped).

    Results: ListProvenanceRecordsForArtifactResultSuccess | ListProvenanceRecordsForArtifactResultFailure
    """

    macro_path: str | MacroPath


@dataclass
@PayloadRegistry.register
class ListProvenanceRecordsForArtifactResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The artifact's history, newest first, as wire headers (payloads summarized)."""

    records: list[ProvenanceRecordHeader] = field(default_factory=list)
    matched_by: ProvenanceMatchOrigin = ProvenanceMatchOrigin.PATH


@dataclass
@PayloadRegistry.register
class ListProvenanceRecordsForArtifactResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The artifact's history could not be listed."""

    failure_reason: ProvenanceQueryFailureReason


@dataclass
@PayloadRegistry.register
class ListProvenanceRecordsForHashRequest(RequestPayload):
    """List every record whose artifact bytes had the given content hash.

    Use when: tooling already holds a hash (e.g. from a source link or an
    external manifest) and wants the records for that exact content,
    regardless of where the files live now.

    Args:
        content_hash: Prefixed content hash ("blake2b-256:<hex>").

    Results: ListProvenanceRecordsForHashResultSuccess | ListProvenanceRecordsForHashResultFailure
    """

    content_hash: str


@dataclass
@PayloadRegistry.register
class ListProvenanceRecordsForHashResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Records for the content hash, newest first, as wire headers (payloads summarized)."""

    records: list[ProvenanceRecordHeader] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListProvenanceRecordsForHashResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Records for the content hash could not be listed."""

    failure_reason: ProvenanceQueryFailureReason


@dataclass
class ProvenancedArtifactSummary:
    """One artifact that has provenance: its identity and history shape.

    Built from the store's directory structure alone (no record parsing), so
    inventories stay cheap even when individual records are large.

    Attributes:
        macro_path: Portable macro form when the location maps into the
            project; otherwise the best available path spelling.
        file_name: The artifact's filename.
        artifact_kind: The claiming provider's friendly name, lowercased
            ("image", "video", ...); None when no provider claims the format.
        record_count: How many save events this artifact has.
        latest_record_id: The newest record's ID (its timestamp prefix is the
            latest save time).
    """

    macro_path: str
    file_name: str
    artifact_kind: str | None
    record_count: int
    latest_record_id: str


@dataclass
@PayloadRegistry.register
class ListProvenancedArtifactsRequest(RequestPayload):
    """Inventory every artifact that has provenance records.

    Use when: presenting "what does this project have history for", grouped
    by artifact type.

    Results are sorted by artifact_kind (None last), then macro_path.

    Results: ListProvenancedArtifactsResultSuccess | ListProvenancedArtifactsResultFailure
    """


@dataclass
@PayloadRegistry.register
class ListProvenancedArtifactsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The store's artifact inventory."""

    artifacts: list[ProvenancedArtifactSummary] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListProvenancedArtifactsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The inventory could not be produced."""

    failure_reason: ProvenanceQueryFailureReason


@dataclass
@PayloadRegistry.register
class GetProvenancePayloadRequest(RequestPayload):
    """Fetch one record's FULL payload: the heavyweight, engine-deserializable truth.

    Use when: actually rehydrating a node or extracting the embedded workflow.
    Every other request returns headers with the payload summarized; this is
    the explicit opt-in to the weight (payloads embed entire workflow .py files).

    Args:
        macro_path: The artifact's location: a MacroPath, or a string
            (workspace-relative, absolute, or macro-shaped).
        record_id: A specific record; None fetches the latest.

    Results: GetProvenancePayloadResultSuccess | GetProvenancePayloadResultFailure
    """

    macro_path: str | MacroPath
    record_id: str | None = None


@dataclass
@PayloadRegistry.register
class GetProvenancePayloadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The record's full payload."""

    record_id: str
    payload: ProvenancePayload


@dataclass
@PayloadRegistry.register
class GetProvenancePayloadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The payload could not be produced."""

    failure_reason: ProvenanceQueryFailureReason
