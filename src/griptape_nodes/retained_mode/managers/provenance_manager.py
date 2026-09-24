"""Provenance capture: immutable per-save records with lineage.

`ProvenanceManager` owns the artifact provenance record store. Capture runs
inside the engine's file-write pipeline (`OSManager.on_write_file_request`
calls `record_artifact_save` once the final path and bytes are known); records
are written through `WriteFileRequest` with `provenance=None`, which is what
keeps record/pointer/snapshot writes out of the capture system without a
recursion guard.

Design: docs/development/designs/artifact_provenance.md (§7, §8).
"""

from __future__ import annotations

import base64
import logging
import pickle
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from griptape.artifacts import UrlArtifact

from griptape_nodes.common.macro_parser import MacroSyntaxError, ParsedMacro
from griptape_nodes.common.project_templates.provenance_settings import (
    ProvenanceCapturePolicy,
    ProvenanceFailurePolicy,
    ProvenanceSettings,
    resolve_capture_policy,
    resolve_failure_policy,
)
from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.files.path_utils import canonicalize_for_identity, decompose_source_path, parse_static_server_url
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.event_converter import safe_unstructure
from griptape_nodes.retained_mode.events.flow_events import (
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    SerializedNodeCommands,
    SerializedParameterValueTracker,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultSuccess,
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.events.provenance_events import (
    GetProvenanceForArtifactRequest,
    GetProvenanceForArtifactResultFailure,
    GetProvenanceForArtifactResultSuccess,
    GetProvenancePayloadRequest,
    GetProvenancePayloadResultFailure,
    GetProvenancePayloadResultSuccess,
    ListProvenancedArtifactsRequest,
    ListProvenancedArtifactsResultFailure,
    ListProvenancedArtifactsResultSuccess,
    ListProvenanceRecordsForArtifactRequest,
    ListProvenanceRecordsForArtifactResultFailure,
    ListProvenanceRecordsForArtifactResultSuccess,
    ListProvenanceRecordsForHashRequest,
    ListProvenanceRecordsForHashResultFailure,
    ListProvenanceRecordsForHashResultSuccess,
    ProvenancedArtifactSummary,
    ProvenanceMatchOrigin,
    ProvenanceQueryFailureReason,
)
from griptape_nodes.retained_mode.file_metadata.provenance_record import (
    BY_PATH_DIR_NAME,
    PARAMETER_VALUES_FORMAT,
    ArtifactIdentity,
    ByHashPointer,
    ProducingNodeIdentity,
    ProducingNodeIdentitySource,
    ProvenanceContent,
    ProvenancePayload,
    ProvenanceRecord,
    ProvenanceRecordHeader,
    ProvenanceRelationship,
    ProvenanceWriteDetails,
    SerializedNodePayload,
    SerializedWorkflowPayload,
    SourceLink,
    WorkflowIdentity,
    by_hash_relative_path,
    dump_pointer_yaml,
    dump_record_yaml,
    generate_record_id,
    hash_content,
    hash_hex_from_content_hash,
    is_reader_compatible,
    load_pointer_yaml,
    load_record_yaml,
)
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import (
    _collect_parameter_values,
    _collect_workflow_info,
)
from griptape_nodes.utils.version_utils import get_current_version

if TYPE_CHECKING:
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

# Longest prompt-ish parameter excerpt allowed into a record's summary_line.
_SUMMARY_PARAM_EXCERPT_CHARS = 60


@dataclass
class ProvenanceCaptureResult:
    """Outcome of one capture attempt.

    Exactly one of three shapes:
    - skipped: details is None and error_message is None (policy resolved to no
      record, or the project template has no provenance situation)
    - success: details is set
    - failure: error_message is set (the failure policy decides what the caller
      does with it)
    """

    details: ProvenanceWriteDetails | None = None
    error_message: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_message is not None


@dataclass
class ArtifactWriteFacts:
    """What OSManager knows about a completed (or staged) artifact write.

    For append writes, `final_content_bytes` is the whole resulting file, not
    the appended chunk, so the record hash always covers the file's content.
    """

    final_file_path: Path
    final_content_bytes: bytes
    append: bool = False
    extension_coerced_from: str | None = None


@dataclass
class ProvenanceCapturePlan:
    """Resolved policies for one save, computed before any bytes move.

    `active` is False when the resolved capture policy records nothing OR the
    project template has no provenance situation (legacy schema) — either way
    the caller can skip capture and never apply the failure policy.
    """

    capture_policy: ProvenanceCapturePolicy
    failure_policy: ProvenanceFailurePolicy
    active: bool


@dataclass
class _ResolvedRecords:
    """An artifact lookup's outcome: the records and how they were found."""

    records: list[ProvenanceRecord]
    matched_by: ProvenanceMatchOrigin


@dataclass
class _ResolvedStorePaths:
    """Absolute locations resolved for one capture: the record file and the store root."""

    record_path: Path
    store_root: Path


class ProvenanceManager(EngineScoped):
    """Writes and (in later phases) queries immutable per-save provenance records.

    Public-surface contract: the public methods here are the ENGINE-INTERNAL
    capture protocol for OSManager's write pipeline (`plan_capture` →
    `preflight_record_dir`/`record_artifact_save` → `rollback_record`), which
    cannot be expressed as independent requests because capture is interleaved
    with the write itself (pre-capture inside the atomic-overwrite window,
    rollback on write failure). Everything OUTSIDE the engine — editor,
    node libraries, external tooling — talks to provenance exclusively through
    the request/event family (`provenance_events.py`, forthcoming), never by
    calling this manager directly.
    """

    def __init__(self, event_manager: EventManager | None = None, *, engine: Engine | None = None) -> None:
        super().__init__(engine=engine)
        self._event_manager = event_manager
        if event_manager is not None:
            event_manager.assign_manager_to_request_type(
                GetProvenanceForArtifactRequest, self.on_get_provenance_for_artifact_request
            )
            event_manager.assign_manager_to_request_type(
                ListProvenanceRecordsForArtifactRequest, self.on_list_provenance_records_for_artifact_request
            )
            event_manager.assign_manager_to_request_type(
                ListProvenanceRecordsForHashRequest, self.on_list_provenance_records_for_hash_request
            )
            event_manager.assign_manager_to_request_type(
                ListProvenancedArtifactsRequest, self.on_list_provenanced_artifacts_request
            )
            event_manager.assign_manager_to_request_type(
                GetProvenancePayloadRequest, self.on_get_provenance_payload_request
            )
        # One-time flag so legacy projects (templates without the provenance
        # situation) log a single info line instead of one per save.
        self._warned_no_situation = False

    def plan_capture(self, provenance: ProvenanceContent, artifact_path: Path) -> ProvenanceCapturePlan:
        """Resolve both policies for a save before any bytes move.

        The write pipeline consults the plan to sequence capture around the
        artifact write (pre-capture for atomic overwrites, append pre-flight)
        and to decide what a capture failure does to the save. The plan is
        inactive when nothing would be recorded: policy resolves to no record,
        the template has no provenance situation (legacy schema), or the
        project's format gate excludes this file's format.
        """
        project_settings = self._current_project_provenance_settings()
        capture_policy = resolve_capture_policy(
            provenance.capture_policy, project_settings, artifact_kind=self._artifact_kind(artifact_path)
        )
        failure_policy = resolve_failure_policy(provenance.failure_policy, project_settings)
        situation_available = self._provenance_situation_macro() is not None
        # Legacy template (v0) with no provenance situation: record nothing,
        # never fail the save. Upgrading the project's template major enables capture.
        capture_wanted = capture_policy != ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
        if capture_wanted and not situation_available and not self._warned_no_situation:
            self._warned_no_situation = True
            logger.info(
                "Provenance capture is unavailable in this project: its template has no "
                "'%s' situation (legacy schema). Files will save normally without provenance records.",
                BuiltInSituation.SAVE_ARTIFACT_PROVENANCE,
            )
        active = (
            capture_policy != ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
            and situation_available
            and not self._is_engine_scratch_path(artifact_path)
        )
        return ProvenanceCapturePlan(capture_policy=capture_policy, failure_policy=failure_policy, active=active)

    def preflight_record_dir(self, artifact_path: Path) -> str | None:
        """Ensure the record directory for an artifact is creatable before a non-rollbackable write.

        Used for append writes under fail_artifact_save: appends cannot be
        rolled back, so the record location is proven writable before any bytes
        are appended. Returns an artist-readable error message, or None when ready.
        """
        try:
            record_dir = self._record_dir_for_path(artifact_path)
            if record_dir is None:
                return None
            record_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"Attempted to prepare the provenance record folder for '{artifact_path.name}'. Failed due to: {e}"
        return None

    def rollback_record(self, details: ProvenanceWriteDetails) -> None:
        """Best-effort removal of a just-written record and its by-hash pointer.

        Used when the artifact write fails after a pre-write capture (atomic
        overwrite ordering): a record must not describe a save that never happened.
        """
        record_path = Path(details.record_path)
        try:
            record_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Provenance: could not roll back record '%s': %s", record_path, e)
        store_root = self._store_root_or_none()
        if store_root is None:
            return
        pointer_path = store_root / by_hash_relative_path(details.content_hash, details.record_id)
        try:
            pointer_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Provenance: could not roll back by-hash pointer '%s': %s", pointer_path, e)

    def record_artifact_save(
        self, facts: ArtifactWriteFacts, provenance: ProvenanceContent, plan: ProvenanceCapturePlan
    ) -> ProvenanceCaptureResult:
        """Capture a provenance record for a just-written artifact.

        Args:
            facts: The write's final truth: resolved path (post collision-walk,
                post extension coercion), definitive bytes, and edge-case flags.
            provenance: The caller's election (policies, node identity, situation).
            plan: The resolved plan from `plan_capture` for this same save; the
                capture uses its policies rather than re-resolving, so the record
                cannot disagree with the gates the write pipeline sequenced around.

        Returns:
            ProvenanceCaptureResult; never raises. The caller applies the
            resolved failure policy to a failed result.
        """
        try:
            return self._record_artifact_save(facts, provenance, plan)
        except Exception as e:
            logger.exception("Provenance capture failed for '%s'", facts.final_file_path)
            return ProvenanceCaptureResult(
                error_message=(
                    f"Attempted to write a provenance record for '{facts.final_file_path.name}'. Failed due to: {e}"
                )
            )

    def find_latest_record_for_path(self, artifact_path: Path) -> ProvenanceRecord | None:
        """Load the latest readable record for an artifact path, or None.

        Latest = lexicographic max of the record directory (record IDs are
        time-sortable). Unreadable or newer-major records are skipped rather
        than raised: the store is user-editable.
        """
        record_dir = self._record_dir_for_path(artifact_path)
        if record_dir is None or not record_dir.is_dir():
            return None
        for record_file in sorted(record_dir.glob("*.yaml"), reverse=True):
            record = self._load_record(record_file)
            if record is not None:
                return record
        return None

    # -- query handlers (the external interface) -----------------------------

    def on_get_provenance_for_artifact_request(self, request: GetProvenanceForArtifactRequest) -> ResultPayload:
        """Get the latest (or a specific) record for an artifact, by any path spelling."""
        artifact_path = self._resolve_artifact_path(request.macro_path)
        if artifact_path is None:
            return GetProvenanceForArtifactResultFailure(
                failure_reason=ProvenanceQueryFailureReason.PATH_UNRESOLVABLE,
                result_details=f"Attempted to look up provenance for '{request.macro_path}'. Failed because the location could not be resolved.",
            )

        resolved = self._records_for_artifact(artifact_path)
        records = resolved.records
        if not records:
            return GetProvenanceForArtifactResultFailure(
                failure_reason=ProvenanceQueryFailureReason.NO_PROVENANCE_RECORDS,
                result_details=f"Attempted to look up provenance for '{request.macro_path}'. Failed because no provenance records exist for it.",
            )

        if request.record_id is not None:
            matches = [record for record in records if record.record_id == request.record_id]
            if not matches:
                return GetProvenanceForArtifactResultFailure(
                    failure_reason=ProvenanceQueryFailureReason.RECORD_NOT_FOUND,
                    result_details=(
                        f"Attempted to look up provenance record '{request.record_id}' for "
                        f"'{request.macro_path}'. Failed because that record does not exist."
                    ),
                )
            record = matches[0]
        else:
            record = records[0]

        record_dir = self._record_dir_for_path(artifact_path)
        record_path = str(record_dir / f"{record.record_id}.yaml") if record_dir is not None else ""
        return GetProvenanceForArtifactResultSuccess(
            record=ProvenanceRecordHeader.from_record(record),
            record_path=record_path,
            matched_by=resolved.matched_by,
            is_stale=self._compute_is_stale(record, artifact_path),
            result_details=f"Found provenance record '{record.record_id}' for '{request.macro_path}'.",
        )

    def on_get_provenance_payload_request(self, request: GetProvenancePayloadRequest) -> ResultPayload:
        """Fetch one record's full payload: the explicit opt-in to the weight."""
        artifact_path = self._resolve_artifact_path(request.macro_path)
        if artifact_path is None:
            return GetProvenancePayloadResultFailure(
                failure_reason=ProvenanceQueryFailureReason.PATH_UNRESOLVABLE,
                result_details=f"Attempted to fetch a provenance payload for '{request.macro_path}'. Failed because the location could not be resolved.",
            )
        records = self._records_for_artifact(artifact_path).records
        if not records:
            return GetProvenancePayloadResultFailure(
                failure_reason=ProvenanceQueryFailureReason.NO_PROVENANCE_RECORDS,
                result_details=f"Attempted to fetch a provenance payload for '{request.macro_path}'. Failed because no provenance records exist for it.",
            )
        if request.record_id is not None:
            matches = [record for record in records if record.record_id == request.record_id]
            if not matches:
                return GetProvenancePayloadResultFailure(
                    failure_reason=ProvenanceQueryFailureReason.RECORD_NOT_FOUND,
                    result_details=(
                        f"Attempted to fetch provenance payload '{request.record_id}' for "
                        f"'{request.macro_path}'. Failed because that record does not exist."
                    ),
                )
            record = matches[0]
        else:
            record = records[0]
        return GetProvenancePayloadResultSuccess(
            record_id=record.record_id,
            payload=record.payload,
            result_details=f"Fetched the full payload of record '{record.record_id}'.",
        )

    def on_list_provenance_records_for_artifact_request(
        self, request: ListProvenanceRecordsForArtifactRequest
    ) -> ResultPayload:
        """List one artifact's save history, newest first."""
        artifact_path = self._resolve_artifact_path(request.macro_path)
        if artifact_path is None:
            return ListProvenanceRecordsForArtifactResultFailure(
                failure_reason=ProvenanceQueryFailureReason.PATH_UNRESOLVABLE,
                result_details=f"Attempted to list provenance for '{request.macro_path}'. Failed because the location could not be resolved.",
            )
        resolved = self._records_for_artifact(artifact_path)
        return ListProvenanceRecordsForArtifactResultSuccess(
            records=[ProvenanceRecordHeader.from_record(record) for record in resolved.records],
            matched_by=resolved.matched_by,
            result_details=f"Found {len(resolved.records)} provenance record(s) for '{request.macro_path}'.",
        )

    def on_list_provenance_records_for_hash_request(
        self, request: ListProvenanceRecordsForHashRequest
    ) -> ResultPayload:
        """List every record for exact content bytes, newest first."""
        store_root = self._store_root_or_none()
        if store_root is None:
            return ListProvenanceRecordsForHashResultFailure(
                failure_reason=ProvenanceQueryFailureReason.STORE_UNAVAILABLE,
                result_details="Attempted to list provenance by content hash. Failed because the provenance store could not be located.",
            )
        try:
            records = self._records_for_hash(request.content_hash, store_root)
        except ValueError as e:
            return ListProvenanceRecordsForHashResultFailure(
                failure_reason=ProvenanceQueryFailureReason.PATH_UNRESOLVABLE,
                result_details=f"Attempted to list provenance by content hash. Failed due to: {e}",
            )
        return ListProvenanceRecordsForHashResultSuccess(
            records=[ProvenanceRecordHeader.from_record(record) for record in records],
            result_details=f"Found {len(records)} provenance record(s) for the content hash.",
        )

    def on_list_provenanced_artifacts_request(self, request: ListProvenancedArtifactsRequest) -> ResultPayload:  # noqa: ARG002
        """Inventory every artifact with provenance, sorted by artifact type then path."""
        store_root = self._store_root_or_none()
        if store_root is None:
            return ListProvenancedArtifactsResultFailure(
                failure_reason=ProvenanceQueryFailureReason.STORE_UNAVAILABLE,
                result_details="Attempted to inventory provenanced artifacts. Failed because the provenance store could not be located.",
            )
        by_path_root = store_root / BY_PATH_DIR_NAME
        summaries: list[ProvenancedArtifactSummary] = []
        if by_path_root.is_dir():
            artifact_dirs: dict[Path, list[str]] = {}
            for record_file in by_path_root.rglob("*.yaml"):
                artifact_dirs.setdefault(record_file.parent, []).append(record_file.stem)
            for artifact_dir, record_ids in artifact_dirs.items():
                summaries.append(self._summarize_artifact_dir(artifact_dir, by_path_root, record_ids))
        summaries.sort(key=lambda s: (s.artifact_kind is None, s.artifact_kind or "", s.macro_path))
        return ListProvenancedArtifactsResultSuccess(
            artifacts=summaries,
            result_details=f"Found {len(summaries)} artifact(s) with provenance.",
        )

    def _record_artifact_save(
        self, facts: ArtifactWriteFacts, provenance: ProvenanceContent, plan: ProvenanceCapturePlan
    ) -> ProvenanceCaptureResult:
        # The plan already gated NO_PROVENANCE_RECORDED, scratch paths, and the
        # legacy-template case; an inactive plan means the caller skipped capture
        # entirely. Guard anyway so a stale plan degrades to "no record".
        if not plan.active:
            return ProvenanceCaptureResult()
        capture_policy = plan.capture_policy

        situation_macro = self._provenance_situation_macro()
        if situation_macro is None:
            # The situation vanished between planning and capture (project
            # switch mid-write); record nothing rather than guess a location.
            return ProvenanceCaptureResult()

        record_id = generate_record_id()
        store_paths = self._resolve_store_paths(facts.final_file_path, record_id, situation_macro)

        producing_node = self._resolve_producing_node(provenance)
        record = self._build_record(
            facts=facts,
            record_id=record_id,
            capture_policy=capture_policy,
            producing_node=producing_node,
            provenance=provenance,
        )
        content_hash = record.artifact.content_hash

        if capture_policy == ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT:
            self._attach_workflow_file(record)

        record_write_error = self._write_record_files(record, store_paths, content_hash, record_id)
        if record_write_error is not None:
            return ProvenanceCaptureResult(error_message=record_write_error)

        return ProvenanceCaptureResult(
            details=ProvenanceWriteDetails(
                record_id=record_id,
                record_path=str(store_paths.record_path),
                content_hash=content_hash,
                capture_policy=capture_policy,
            )
        )

    def _artifact_kind(self, artifact_path: Path) -> str | None:
        """The claiming artifact provider's friendly name, lowercased, or None if unclaimed."""
        extension = artifact_path.suffix.lstrip(".").lower()
        if not extension:
            return None
        registry = self.engine.artifact_manager._registry
        provider_classes = registry.get_provider_classes_by_format(extension)
        if not provider_classes:
            return None
        return provider_classes[0].get_friendly_name().lower()

    def _is_engine_scratch_path(self, artifact_path: Path) -> bool:
        """Whether a save landed in the OS temp root OUTSIDE the workspace.

        Scratch staging files are transient by definition, so their records
        would be guaranteed-dangling noise. Both conditions matter: a workspace
        deliberately placed under the temp root (ephemeral/CI setups) still
        captures, and user-chosen out-of-workspace destinations (not in temp)
        capture normally.
        """
        try:
            temp_root = canonicalize_for_identity(Path(tempfile.gettempdir()))
            candidate = canonicalize_for_identity(artifact_path)
        except (OSError, RuntimeError, ValueError):
            return False
        if not candidate.is_relative_to(temp_root):
            return False
        workspace_dir = self._current_workspace_dir()
        if workspace_dir is None:
            return True
        try:
            workspace = canonicalize_for_identity(workspace_dir)
        except (OSError, RuntimeError, ValueError):
            return True
        return not candidate.is_relative_to(workspace)

    def _summarize_artifact_dir(
        self, artifact_dir: Path, by_path_root: Path, record_ids: list[str]
    ) -> ProvenancedArtifactSummary:
        """Build an inventory entry from directory structure alone (no record parsing)."""
        mirror_relative = artifact_dir.relative_to(by_path_root)
        reconstructed = self._reconstruct_artifact_path(mirror_relative)
        macro_path: str | None = None
        if reconstructed is not None:
            macro_path = self._macro_path_for(reconstructed)
        best_path = macro_path or (str(reconstructed) if reconstructed is not None else mirror_relative.as_posix())
        return ProvenancedArtifactSummary(
            macro_path=best_path,
            file_name=artifact_dir.name,
            artifact_kind=self._artifact_kind(Path(artifact_dir.name)),
            record_count=len(record_ids),
            latest_record_id=max(record_ids),
        )

    def _reconstruct_artifact_path(self, mirror_relative: Path) -> Path | None:
        """Best-effort inverse of the by-path mirroring for one artifact directory.

        In-workspace saves mirror workspace-relative, so joining onto the
        workspace reproduces them. Outside-workspace saves mirror under a
        drive/volume segment; reconstruction is heuristic and returns None
        rather than guessing wrong.
        """
        workspace_dir = self._current_workspace_dir()
        if workspace_dir is not None:
            candidate = workspace_dir / mirror_relative
            if candidate.exists() or not mirror_relative.parts:
                return candidate
        first = mirror_relative.parts[0] if mirror_relative.parts else ""
        if len(first) == 2 and first.endswith(":"):  # noqa: PLR2004
            return Path(first + "/") / Path(*mirror_relative.parts[1:])
        rooted = Path("/") / mirror_relative
        if rooted.exists():
            return rooted
        if workspace_dir is not None:
            # The artifact may simply have been deleted; prefer the workspace shape.
            return workspace_dir / mirror_relative
        return None

    def _resolve_artifact_path(self, macro_path: str | MacroPath) -> Path | None:
        """Resolve a `str | MacroPath` location (the OS request surface's type) to an absolute path.

        A MacroPath carries macro-ness in its type. A string is a plain path,
        except that macro-shaped strings are upgraded exactly the way
        File.__init__ upgrades them at its boundary: parse, don't peek -- the
        macro parser decides. (File.resolve() itself is off limits here: it
        routes through the GriptapeNodes facade / current_engine, and
        engine-internal code stays scoped to self.engine.)
        """
        if isinstance(macro_path, MacroPath):
            return self._resolve_macro(macro_path.parsed_macro, dict(macro_path.variables))
        if not macro_path:
            return None
        try:
            parsed = ParsedMacro(macro_path)
        except MacroSyntaxError:
            parsed = None
        if parsed is not None and parsed.get_variables():
            return self._resolve_macro(parsed, {})
        candidate = Path(macro_path)
        if candidate.is_absolute():
            return candidate
        workspace_dir = self._current_workspace_dir()
        if workspace_dir is None:
            return None
        return workspace_dir / candidate

    def _resolve_macro(self, parsed: ParsedMacro, variables: dict) -> Path | None:
        path_result = self.engine.handle_request(GetPathForMacroRequest(parsed_macro=parsed, variables=variables))
        if not isinstance(path_result, GetPathForMacroResultSuccess):
            return None
        return path_result.absolute_path

    def _records_for_artifact(self, artifact_path: Path) -> _ResolvedRecords:
        """All readable records for an artifact, newest first; by-hash fallback on a path miss."""
        records: list[ProvenanceRecord] = []
        record_dir = self._record_dir_for_path(artifact_path)
        if record_dir is not None and record_dir.is_dir():
            for record_file in sorted(record_dir.glob("*.yaml"), reverse=True):
                record = self._load_record(record_file)
                if record is not None:
                    records.append(record)
        if records:
            return _ResolvedRecords(records=records, matched_by=ProvenanceMatchOrigin.PATH)
        # Path miss: a moved/renamed/hand-copied file can still find its history
        # by content.
        empty = _ResolvedRecords(records=[], matched_by=ProvenanceMatchOrigin.PATH)
        try:
            live_hash = hash_content(artifact_path.read_bytes())
        except OSError:
            return empty
        store_root = self._store_root_or_none()
        if store_root is None:
            return empty
        try:
            hash_records = self._records_for_hash(live_hash, store_root)
        except ValueError:
            return empty
        return _ResolvedRecords(records=hash_records, matched_by=ProvenanceMatchOrigin.HASH)

    def _records_for_hash(self, content_hash: str, store_root: Path) -> list[ProvenanceRecord]:
        """All readable records reachable through the hash's pointers, newest first.

        Raises:
            ValueError: If the content hash is not in the expected prefixed form.
        """
        pointer_dir = store_root / by_hash_relative_path(content_hash, "_").rsplit("/", 1)[0]
        records: list[ProvenanceRecord] = []
        if not pointer_dir.is_dir():
            return records
        for pointer_file in sorted(pointer_dir.glob("*.yaml"), reverse=True):
            try:
                pointer = load_pointer_yaml(pointer_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.debug("Provenance: skipping unreadable by-hash pointer '%s': %s", pointer_file, e)
                continue
            record = self._load_record(store_root / pointer.record)
            if record is not None:
                records.append(record)
        return records

    def _compute_is_stale(self, record: ProvenanceRecord, artifact_path: Path) -> bool | None:
        """Whether the artifact's live bytes still match the record; None when unjudgeable."""
        live_path = Path(record.artifact.path_at_save)
        if not live_path.exists():
            live_path = artifact_path
        try:
            live_hash = hash_content(live_path.read_bytes())
        except OSError:
            return None
        return live_hash != record.artifact.content_hash

    # -- capture internals -------------------------------------------------

    def _current_project_provenance_settings(self) -> ProvenanceSettings | None:
        project_result = self.engine.handle_request(GetCurrentProjectRequest())
        if not isinstance(project_result, GetCurrentProjectResultSuccess):
            return None
        return project_result.project_info.template.provenance

    def _current_workspace_dir(self) -> Path | None:
        """The workspace root: the anchor for record-path mirroring and relative paths.

        Deliberately the configured workspace (what StaticFilesManager anchors
        to), NOT the project file's directory (`project_base_dir`): artifacts
        and the provenance store both live under the workspace, and anchoring
        decomposition to the project-file dir mirrored in-workspace saves as
        absolute paths (by-path/Users/...) whenever the project file lived
        elsewhere.
        """
        try:
            return self.engine.config_manager.workspace_path
        except Exception:
            return None

    def _provenance_situation_macro(self) -> str | None:
        situation_result = self.engine.handle_request(
            GetSituationRequest(situation_name=BuiltInSituation.SAVE_ARTIFACT_PROVENANCE)
        )
        if not isinstance(situation_result, GetSituationResultSuccess):
            return None
        return situation_result.situation.macro

    def _resolve_store_paths(self, final_file_path: Path, record_id: str, situation_macro: str) -> _ResolvedStorePaths:
        """Resolve the record's absolute path and the store root for derived paths.

        Raises:
            RuntimeError: If the project is not loaded or macro resolution fails.
        """
        record_path = self._resolve_record_path(final_file_path, record_id, situation_macro)

        store_root_result = self.engine.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{griptape-nodes-provenance}"), variables={})
        )
        if not isinstance(store_root_result, GetPathForMacroResultSuccess):
            msg = f"Failed to resolve the provenance store root: {store_root_result.result_details}"
            raise RuntimeError(msg)  # noqa: TRY004
        return _ResolvedStorePaths(record_path=record_path, store_root=store_root_result.absolute_path)

    def _resolve_record_path(self, artifact_path: Path, record_id: str, situation_macro: str) -> Path:
        """Resolve where a record for `artifact_path` with `record_id` lives.

        Raises:
            RuntimeError: If the project is not loaded or macro resolution fails.
        """
        workspace_dir = self._current_workspace_dir()
        if workspace_dir is None:
            msg = "No current project loaded"
            raise RuntimeError(msg)

        decomposed = decompose_source_path(artifact_path, workspace_dir)
        variables: dict[str, str | int] = {
            "source_file_name": decomposed.source_file_name,
            "provenance_record_id": record_id,
        }
        if decomposed.drive_volume_mount:
            variables["drive_volume_mount"] = decomposed.drive_volume_mount
        if decomposed.source_relative_path:
            variables["source_relative_path"] = decomposed.source_relative_path

        path_result = self.engine.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro(situation_macro), variables=variables)
        )
        if not isinstance(path_result, GetPathForMacroResultSuccess):
            msg = f"Failed to resolve provenance record path: {path_result.result_details}"
            raise RuntimeError(msg)  # noqa: TRY004
        return path_result.absolute_path

    def _record_dir_for_path(self, artifact_path: Path) -> Path | None:
        """Compute the record directory for an artifact path (for reads), or None if unavailable."""
        situation_macro = self._provenance_situation_macro()
        if situation_macro is None:
            return None
        try:
            # Resolve with a placeholder ID and take the parent: readers honor a
            # customized macro the same way writers do.
            placeholder = self._resolve_record_path(artifact_path, "_", situation_macro)
        except RuntimeError:
            return None
        return placeholder.parent

    def _resolve_producing_node(self, provenance: ProvenanceContent) -> ProducingNodeIdentity | None:
        if provenance.producing_node is not None:
            return provenance.producing_node

        # Inference fallback: today's resolving-node heuristic, flagged so record
        # consumers can distinguish attested identity from a guess.
        context_manager = self.engine.context_manager
        if not context_manager.has_current_flow():
            return None
        flow = context_manager.get_current_flow()
        _, resolving_nodes, _ = self.engine.flow_manager.flow_state(flow)
        if not resolving_nodes:
            return None
        node_name = resolving_nodes[0]
        node_type = self._node_type_name(node_name)
        return ProducingNodeIdentity(
            name=node_name,
            node_type=node_type or node_name,
            identity_source=ProducingNodeIdentitySource.INFERRED_RESOLVING_NODE,
        )

    def _node_type_name(self, node_name: str) -> str | None:
        try:
            node = self.engine.object_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
        except Exception:
            return None
        if node is None:
            return None
        return type(node).__name__

    def _build_record(
        self,
        *,
        facts: ArtifactWriteFacts,
        record_id: str,
        capture_policy: ProvenanceCapturePolicy,
        producing_node: ProducingNodeIdentity | None,
        provenance: ProvenanceContent,
    ) -> ProvenanceRecord:
        final_file_path = facts.final_file_path
        content_hash = hash_content(facts.final_content_bytes)
        artifact = ArtifactIdentity(
            path_at_save=str(final_file_path),
            macro_path=self._macro_path_for(final_file_path),
            file_name=final_file_path.name,
            content_hash=content_hash,
            size_bytes=len(facts.final_content_bytes),
            append=facts.append,
            extension_coerced_from=facts.extension_coerced_from,
        )

        parameters_preview: dict[str, Any] | None = None
        parameters_omitted: list[str] | None = None
        payload = ProvenancePayload()
        if producing_node is not None:
            # Preview: the LOSSY display projection (existing safe_unstructure
            # path, honoring exclude_from_metadata). Never the truth.
            collection = _collect_parameter_values(producing_node.name, self.engine)
            if collection is not None:
                parameters_preview = collection.values or None
                parameters_omitted = collection.omitted or None
            # Truth: the full serialize-node protocol.
            payload = self._serialize_node_payload(producing_node.name)

        workflow = self._collect_workflow_identity()

        record = ProvenanceRecord(
            record_id=record_id,
            saved_at=datetime.now(UTC).isoformat(),
            capture_policy=capture_policy,
            relationship=ProvenanceRelationship.PRODUCED,
            engine_version=self._engine_version(),
            artifact=artifact,
            producing_node=producing_node,
            parameters_preview=parameters_preview,
            parameters_omitted=parameters_omitted,
            workflow=workflow,
            situation=provenance.situation,
            sources=self._discover_sources(producing_node),
            payload=payload,
        )
        record.summary_line = self._compose_summary_line(record)
        return record

    def _macro_path_for(self, final_file_path: Path) -> str | None:
        """The portable macro form of the saved path (e.g. '{outputs}/hero.png'), when one exists.

        Uses the same mapping ProjectFileDestination applies after writes, so a
        record's location can re-resolve against the current project on any
        machine even when the absolute path differs.
        """
        map_result = self.engine.handle_request(AttemptMapAbsolutePathToProjectRequest(absolute_path=final_file_path))
        if isinstance(map_result, AttemptMapAbsolutePathToProjectResultSuccess):
            return map_result.mapped_path
        return None

    @staticmethod
    def _engine_version() -> str | None:
        try:
            return get_current_version()
        except Exception:
            return None

    def _collect_workflow_identity(self) -> WorkflowIdentity | None:
        context_manager = self.engine.context_manager
        if not context_manager.has_current_workflow():
            return None
        try:
            workflow_name = context_manager.get_current_workflow_name()
        except Exception:
            return None
        info = _collect_workflow_info(workflow_name)
        flow_name: str | None = None
        if context_manager.has_current_flow():
            try:
                flow_name = context_manager.get_current_flow().name
            except Exception:
                flow_name = None
        return WorkflowIdentity(
            name=info.get("name"),
            flow_name=flow_name,
            created=info.get("created"),
            modified=info.get("modified"),
            engine_version_created_with=info.get("engine_version"),
        )

    def _serialize_node_payload(self, node_name: str) -> ProvenancePayload:
        """Capture the producing node via the FULL serialize-node protocol.

        The unique parameter values live in the dict passed INTO the request
        (filled in-place); recording the commands without that dict would
        produce references to values that don't exist. This is the same
        protocol workflow save uses: commands + indirect set-value commands +
        the pickled values dict, deserializable with existing machinery.
        """
        unique_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any] = {}
        tracker = SerializedParameterValueTracker()
        serialize_result = self.engine.handle_request(
            SerializeNodeToCommandsRequest(
                node_name=node_name,
                unique_parameter_uuid_to_values=unique_values,
                serialized_parameter_value_tracker=tracker,
                use_pickling=True,
                serialize_all_parameter_values=True,
            )
        )
        if not isinstance(serialize_result, SerializeNodeToCommandsResultSuccess):
            logger.warning("Provenance: failed to serialize node '%s' commands", node_name)
            return ProvenancePayload()
        try:
            # TYPED access, deliberately not to_json()+dict.get(): if the
            # serialize-node protocol renames or reshapes its outputs, this
            # breaks loudly at type-check instead of silently storing empty
            # payloads. See the companion note on SerializedNodeCommands.
            commands_dict = safe_unstructure(serialize_result.serialized_node_commands)
            set_value_dicts = safe_unstructure(serialize_result.set_parameter_value_commands)
            # The element-modification command list is polymorphic, so the
            # unstructured dicts are readable but not faithfully restructurable.
            # Pickle is the faithful transport, exactly as the clipboard path does it.
            pickled_commands = base64.b64encode(pickle.dumps(serialize_result.serialized_node_commands)).decode("ascii")
            pickled_values = base64.b64encode(pickle.dumps(unique_values)).decode("ascii")
        except Exception as e:
            logger.warning("Provenance: failed to encode node '%s' payload: %s", node_name, e)
            return ProvenancePayload()
        return ProvenancePayload(
            serialized_node=SerializedNodePayload(
                serialized_node_commands=commands_dict,
                pickled_node_commands=pickled_commands,
                set_parameter_value_commands=set_value_dicts or [],
                pickled_parameter_values=pickled_values,
                parameter_values_format=PARAMETER_VALUES_FORMAT,
            )
        )

    def _compose_summary_line(self, record: ProvenanceRecord) -> str:
        node_part = "unknown node"
        if record.producing_node is not None:
            node = record.producing_node
            library_part = ""
            if node.library_name:
                version_part = f" {node.library_version}" if node.library_version else ""
                library_part = f", {node.library_name}{version_part}"
            node_part = f"{node.name} [{node.node_type}{library_part}]"

        workflow_part = ""
        if record.workflow is not None and record.workflow.name:
            workflow_part = f" in '{record.workflow.name}'"

        param_part = ""
        excerpt_source = self._summary_excerpt_source(record.parameters_preview)
        if excerpt_source is not None:
            excerpt_name, excerpt_value = excerpt_source
            excerpt = str(excerpt_value)
            if len(excerpt) > _SUMMARY_PARAM_EXCERPT_CHARS:
                excerpt = excerpt[: _SUMMARY_PARAM_EXCERPT_CHARS - 1] + "…"
            param_part = f" — {excerpt_name}='{excerpt}'"

        target = record.artifact.macro_path or record.artifact.file_name
        return f"{node_part}{workflow_part}{param_part} → {target}"

    @staticmethod
    def _summary_excerpt_source(preview: dict[str, Any] | None) -> tuple[str, Any] | None:
        """Pick the preview entry worth excerpting into the summary line.

        Prefer a parameter literally named 'prompt', then the longest
        substantial string value (the prompt-shaped thing artists recall runs
        by), then the first entry; a boolean like api_key_provider makes a
        useless summary.
        """
        if not preview:
            return None
        if "prompt" in preview:
            return ("prompt", preview["prompt"])
        substantial_strings = [
            (name, value)
            for name, value in preview.items()
            if isinstance(value, str) and len(value) >= 8  # noqa: PLR2004
        ]
        if substantial_strings:
            return max(substantial_strings, key=lambda item: len(item[1]))
        return next(iter(preview.items()))

    # -- source discovery ----------------------------------------------------

    def _discover_sources(self, producing_node: ProducingNodeIdentity | None) -> list[SourceLink]:
        """Discover the file-backed sources of the producing node and link their records.

        Never raises and never fails the save: any single source's discovery
        error degrades to a chain-break link with a readable cause.
        """
        if producing_node is None:
            return []
        try:
            node = self.engine.object_manager.attempt_get_object_by_name_as_type(producing_node.name, BaseNode)
        except Exception:
            return []
        if node is None:
            return []

        sources: list[SourceLink] = []
        for parameter, value in self._iter_input_values(node):
            # Classification runs INSIDE the guard: probing whether a value is a
            # file can itself blow up on hostile-shaped strings (a multi-KB
            # prompt stats as ENAMETOOLONG rather than "not a file"), and a
            # value we couldn't even classify is no parent at all — not a break.
            try:
                candidate_path = self._classify_file_backed_value(value, parameter)
            except Exception as e:
                logger.debug(
                    "Provenance: could not classify parameter '%s' value during source discovery: %s",
                    parameter.name,
                    e,
                )
                continue
            if candidate_path is None:
                continue
            try:
                sources.append(self._link_source(candidate_path, parameter.name))
            except Exception as e:
                sources.append(
                    SourceLink(
                        path_at_use=str(candidate_path),
                        macro_path=self._macro_path_for(candidate_path),
                        parameter_name=parameter.name,
                        discovery_error=f"Could not read this source's provenance: {e}",
                    )
                )
        return sources

    def _iter_input_values(self, node: BaseNode) -> list[tuple[Parameter, Any]]:
        """Enumerate INPUT/PROPERTY parameter values, flattening one level of containers.

        Yields the Parameter object with each value: classification consults the
        parameter's DECLARED types, not just the value's runtime type.
        """
        entries: list[tuple[Parameter, Any]] = []
        for param in node.parameters:
            if ParameterMode.INPUT not in param.allowed_modes and ParameterMode.PROPERTY not in param.allowed_modes:
                continue
            try:
                value = node.get_parameter_value(param.name)
            except Exception as e:
                logger.debug("Provenance: skipping parameter '%s' during source discovery: %s", param.name, e)
                continue
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                entries.extend((param, item) for item in value)
            elif isinstance(value, dict):
                entries.extend((param, item) for item in value.values())
            else:
                entries.append((param, value))
        return entries

    def _classify_file_backed_value(self, value: Any, parameter: Parameter) -> Path | None:
        """Map a typed artifact value to its local backing file, or None.

        Classification is strictly type-driven, with the type system speaking
        two ways: the VALUE's runtime type (UrlArtifact and subclasses), or the
        holding PARAMETER's declared types (a string sitting in a parameter
        declared as an artifact type is, by declaration, a file reference).
        A string in a str-typed parameter is prose (a prompt, a caption) and is
        never probed against the filesystem — the ENAMETOOLONG incident rule.
        The probe itself stays exception-guarded either way: junk in a
        declared-artifact parameter degrades to "not a parent", never a failure.
        """
        workspace_dir = self._current_workspace_dir()
        if isinstance(value, UrlArtifact):
            return self._classify_url_artifact(value, workspace_dir)
        if isinstance(value, str) and self._parameter_declares_artifact_type(parameter):
            return self._classify_declared_path_string(value, workspace_dir)
        return None

    @staticmethod
    def _parameter_declares_artifact_type(parameter: Parameter) -> bool:
        """Whether the parameter's declared types name any artifact type.

        Any declared type ending in 'Artifact' (ImageUrlArtifact, VideoArtifact,
        BlobArtifact, ...) counts: the declaration is the type system's testimony
        that values here reference artifacts, whatever their runtime shape.
        """
        declared: list[str] = []
        if parameter.type:
            declared.append(parameter.type)
        if parameter.output_type:
            declared.append(parameter.output_type)
        if parameter.input_types:
            declared.extend(parameter.input_types)
        return any(type_name.endswith("Artifact") for type_name in declared)

    @staticmethod
    def _classify_declared_path_string(value: str, workspace_dir: Path | None) -> Path | None:
        if not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            if workspace_dir is None:
                return None
            candidate = workspace_dir / candidate
        # Guarded probe: even a declared-artifact parameter can hold junk, and
        # an over-long name raises (ENAMETOOLONG) instead of answering "no".
        try:
            if not candidate.is_file():
                return None
        except OSError:
            return None
        return candidate

    @staticmethod
    def _classify_url_artifact(value: UrlArtifact, workspace_dir: Path | None) -> Path | None:
        if workspace_dir is None:
            return None
        resolved = parse_static_server_url(value.value, workspace_dir)
        if resolved is None or not resolved.is_file():
            return None
        return resolved

    def _link_source(self, source_path: Path, parameter_name: str) -> SourceLink:
        source_hash = hash_content(source_path.read_bytes())
        source_macro = self._macro_path_for(source_path)

        latest = self.find_latest_record_for_path(source_path)
        if latest is not None:
            return SourceLink(
                path_at_use=str(source_path),
                macro_path=source_macro,
                parameter_name=parameter_name,
                record_id=latest.record_id,
                content_hash=source_hash,
                matches_latest_record=latest.artifact.content_hash == source_hash,
                summary=latest.summary_line,
            )

        # No records at this path: try by-hash before declaring a chain break,
        # recovering inputs that were renamed or copied outside the engine.
        by_hash_record = self._find_record_by_hash(source_hash)
        if by_hash_record is not None:
            return SourceLink(
                path_at_use=str(source_path),
                macro_path=source_macro,
                parameter_name=parameter_name,
                record_id=by_hash_record.record_id,
                content_hash=source_hash,
                matches_latest_record=True,
                summary=by_hash_record.summary_line,
            )

        return SourceLink(
            path_at_use=str(source_path),
            macro_path=source_macro,
            parameter_name=parameter_name,
            content_hash=source_hash,
        )

    def _find_record_by_hash(self, content_hash: str) -> ProvenanceRecord | None:
        store_root = self._store_root_or_none()
        if store_root is None:
            return None
        hex_digest = hash_hex_from_content_hash(content_hash)
        pointer_dir = store_root / by_hash_relative_path(content_hash, "_").rsplit("/", 1)[0]
        if not pointer_dir.is_dir():
            return None
        for pointer_file in sorted(pointer_dir.glob("*.yaml"), reverse=True):
            try:
                pointer = load_pointer_yaml(pointer_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.debug("Provenance: skipping unreadable by-hash pointer '%s': %s", pointer_file, e)
                continue
            record = self._load_record(store_root / pointer.record)
            if record is not None:
                return record
        logger.debug("Provenance: by-hash pointers for %s resolved no readable record", hex_digest)
        return None

    def _store_root_or_none(self) -> Path | None:
        result = self.engine.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro("{griptape-nodes-provenance}"), variables={})
        )
        if not isinstance(result, GetPathForMacroResultSuccess):
            return None
        return result.absolute_path

    def _load_record(self, record_file: Path) -> ProvenanceRecord | None:
        try:
            raw = record_file.read_text(encoding="utf-8")
            record = load_record_yaml(raw)
        except Exception as e:
            logger.warning("Provenance: skipping unreadable record '%s': %s", record_file, e)
            return None
        if not is_reader_compatible(record.schema_version):
            logger.warning(
                "Provenance: record '%s' has schema version %s, written by a newer engine; skipping",
                record_file,
                record.schema_version,
            )
            return None
        return record

    # -- payload + writes ---------------------------------------------------

    def _attach_workflow_file(self, record: ProvenanceRecord) -> None:
        """Embed the entire workflow as runnable .py file text in the record's payload.

        The .py format is the engine's one versioned, migration-supported
        container -- extract the string to disk and any engine loads it. Runs
        per capture (no memo): a stale cached snapshot would embed the WRONG
        workflow after a mid-session edit, and correctness beats the codegen
        cost. Best-effort: a record without the workflow file is still a valid
        NODE_COMMANDS-level record.
        """
        context_manager = self.engine.context_manager
        if not context_manager.has_current_flow():
            return
        flow: ControlFlow = context_manager.get_current_flow()

        serialize_result = self.engine.handle_request(
            SerializeFlowToCommandsRequest(flow_name=flow.name, include_create_flow_command=False)
        )
        if not isinstance(serialize_result, SerializeFlowToCommandsResultSuccess):
            logger.warning("Provenance: failed to serialize flow '%s' for the workflow snapshot", flow.name)
            return

        workflow_name = flow.name
        if record.workflow is not None and record.workflow.name:
            workflow_name = record.workflow.name

        try:
            content = self.engine.workflow_manager.render_workflow_file_content(
                serialized_flow_commands=serialize_result.serialized_flow_commands,
                file_name=workflow_name,
            )
        except Exception as e:
            logger.warning("Provenance: failed to render workflow '%s' file content: %s", workflow_name, e)
            return

        record.payload.serialized_workflow = SerializedWorkflowPayload(
            workflow_file_content=content,
            workflow_file_hash=hash_content(content.encode("utf-8")),
        )

    def _write_record_files(
        self,
        record: ProvenanceRecord,
        store_paths: _ResolvedStorePaths,
        content_hash: str,
        record_id: str,
    ) -> str | None:
        """Write the canonical record and its by-hash pointer. Returns an error message on failure."""
        record_yaml = dump_record_yaml(record)
        record_result = self.engine.handle_request(
            WriteFileRequest(
                file_path=str(store_paths.record_path),
                content=record_yaml,
                existing_file_policy=ExistingFilePolicy.FAIL,
                skip_metadata_injection=True,
            )
        )
        if not isinstance(record_result, WriteFileResultSuccess):
            return (
                f"Attempted to write the provenance record for '{record.artifact.file_name}'. "
                f"Failed due to: {record_result.result_details}"
            )

        try:
            record_relative = store_paths.record_path.relative_to(store_paths.store_root).as_posix()
        except ValueError:
            # A customized macro placed records outside the store root; the pointer
            # keeps an absolute path so hash lookups still resolve.
            record_relative = store_paths.record_path.as_posix()

        pointer = ByHashPointer(
            record=record_relative,
            artifact_path=record.artifact.macro_path or record.artifact.path_at_save,
        )
        pointer_path = store_paths.store_root / by_hash_relative_path(content_hash, record_id)
        pointer_result = self.engine.handle_request(
            WriteFileRequest(
                file_path=str(pointer_path),
                content=dump_pointer_yaml(pointer),
                existing_file_policy=ExistingFilePolicy.FAIL,
                skip_metadata_injection=True,
            )
        )
        if not isinstance(pointer_result, WriteFileResultSuccess):
            return (
                f"Attempted to write the provenance hash pointer for '{record.artifact.file_name}'. "
                f"Failed due to: {pointer_result.result_details}"
            )
        return None
