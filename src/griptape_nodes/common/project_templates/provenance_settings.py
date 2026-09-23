"""Provenance policy enums and project-level provenance settings.

These live at the project-template layer (not retained_mode) because they are
project-scoped defaults that must travel with projects and published packages
via the template overlay/merge machinery. The provenance record models that
consume them live in `retained_mode/file_metadata/provenance_record.py`.

Design: docs/development/designs/artifact_provenance.md (§6).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ProvenanceCapturePolicy(StrEnum):
    """What a provenance record captures for a given save.

    Deliberately a StrEnum rather than a bool: capture has more than two
    meaningful states and is expected to grow more.
    """

    # Defer to the project template's ProvenanceSettings block (or, when the
    # template has none, the engine defaults). The starting value on every
    # request; resolution always terminates because a project context always
    # exists.
    INHERIT_PROJECT_POLICY = "inherit_project_policy"
    # Write no record for this save. Distinct from omitting the election
    # entirely: the caller is deliberately opting this save out, which also
    # short-circuits the failure policy (nothing to fail over).
    NO_PROVENANCE_RECORDED = "no_provenance_recorded"
    # Record the human-readable envelope (artifact identity + content hash,
    # producing node, parameter summary, parent links) plus the producing
    # node's engine-serialized commands inline. Answers "what made this file,
    # from what, with what settings."
    PRODUCING_NODE_ONLY = "producing_node_only"
    # Everything PRODUCING_NODE_ONLY records, plus the entire workflow embedded
    # in the record as the text of a runnable .py workflow file. Answers "give
    # me back the whole rig that produced this." Strict superset: the envelope
    # shape never changes between levels, only the payload block grows.
    FULL_WORKFLOW_SNAPSHOT = "full_workflow_snapshot"


class ProvenanceFailurePolicy(StrEnum):
    """What happens to the artifact save when an elected record cannot be written."""

    # Defer to the project template's ProvenanceSettings block (or the engine
    # default when the template has none).
    INHERIT_PROJECT_POLICY = "inherit_project_policy"
    # Provenance is load-bearing: if the record cannot be written, the artifact
    # save fails (rolled back where the write mode permits — atomic overwrites
    # leave the prior file untouched, exclusive creates are unlinked; appends
    # are pre-flighted instead because they cannot be rolled back).
    FAIL_ARTIFACT_SAVE = "fail_artifact_save"
    # Provenance is best-effort: the save stands, the record failure is logged
    # and surfaced on the write result's details.
    WARN_AND_CONTINUE = "warn_and_continue"


# Terminal defaults when neither the request nor the project template resolves
# a policy. The per-type table IS the whole capture story: a kind not in the
# table records nothing (explicit per-save elections still win). The engine
# default table covers the shipped artifact providers at full capture -- the
# deliberate provenance-by-default, load-bearing-unless-loosened posture.
# Formats no provider claims can never appear in a kind-keyed table, so they
# record nothing via INHERIT by design (field testing showed an
# everything-records posture fills the store with workflow-file and
# scratch-output noise).
ENGINE_DEFAULT_FAILURE_POLICY = ProvenanceFailurePolicy.FAIL_ARTIFACT_SAVE
ENGINE_DEFAULT_PER_ARTIFACT_TYPE: dict[str, ProvenanceCapturePolicy] = {
    "image": ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT,
    "video": ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT,
    "audio": ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT,
}


class ProvenanceSettings(BaseModel):
    """Project-level provenance defaults, declared in the project template.

    A save whose request policy is INHERIT_PROJECT_POLICY resolves against this
    block; a template with no block falls through to the engine defaults.
    """

    default_failure_policy: ProvenanceFailurePolicy = Field(
        default=ENGINE_DEFAULT_FAILURE_POLICY,
        description="Failure policy applied when a save requests inherit_project_policy",
    )
    per_artifact_type: dict[str, ProvenanceCapturePolicy] = Field(
        # The engine default table, not an empty dict: a project block that only
        # sets failure policy must not silently kill capture. Specifying the
        # table replaces this wholesale.
        default_factory=lambda: dict(ENGINE_DEFAULT_PER_ARTIFACT_TYPE),
        description=(
            "THE capture policy: keyed by the claiming provider's friendly name, "
            "lowercased (e.g. 'image', 'video', 'audio'). A type not in the table "
            "records nothing; formats no provider claims never record via inherit. "
            "Governs INHERIT resolution only: an explicit per-save election always wins."
        ),
    )

    @field_validator("per_artifact_type")
    @classmethod
    def validate_per_type_entries(cls, v: dict[str, ProvenanceCapturePolicy]) -> dict[str, ProvenanceCapturePolicy]:
        """Normalize keys to lowercase and reject inherit_project_policy entries."""
        normalized: dict[str, ProvenanceCapturePolicy] = {}
        for key, policy in v.items():
            if policy == ProvenanceCapturePolicy.INHERIT_PROJECT_POLICY:
                msg = f"per_artifact_type['{key}'] cannot be 'inherit_project_policy'; the project table is what saves inherit from"
                raise ValueError(msg)
            normalized[key.lower()] = policy
        return normalized

    @field_validator("default_failure_policy")
    @classmethod
    def validate_failure_policy_is_terminal(cls, v: ProvenanceFailurePolicy) -> ProvenanceFailurePolicy:
        """Reject inherit_project_policy — the project block IS what saves inherit from."""
        if v == ProvenanceFailurePolicy.INHERIT_PROJECT_POLICY:
            msg = "default_failure_policy cannot be 'inherit_project_policy'; the project default is what saves inherit from"
            raise ValueError(msg)
        return v


def resolve_capture_policy(
    requested: ProvenanceCapturePolicy | None,
    project_settings: ProvenanceSettings | None,
    *,
    artifact_kind: str | None,
) -> ProvenanceCapturePolicy:
    """Resolve a requested capture policy: explicit request, else the per-type table.

    `requested=None` is the idiomatic request-side spelling of INHERIT.
    `artifact_kind` is the claiming provider's friendly name lowercased, or
    None when no provider claims the format. A kind absent from the table (or
    an unclaimed format, which can never be in a kind-keyed table) records
    nothing. The table governs INHERIT resolution only: an explicit per-save
    election always wins.
    """
    if requested is not None and requested != ProvenanceCapturePolicy.INHERIT_PROJECT_POLICY:
        return requested
    if artifact_kind is None:
        return ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
    table = project_settings.per_artifact_type if project_settings is not None else ENGINE_DEFAULT_PER_ARTIFACT_TYPE
    return table.get(artifact_kind, ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED)


def resolve_failure_policy(
    requested: ProvenanceFailurePolicy | None,
    project_settings: ProvenanceSettings | None,
) -> ProvenanceFailurePolicy:
    """Resolve a requested failure policy: request -> project -> engine default.

    `requested=None` is the idiomatic request-side spelling of INHERIT.
    """
    if requested is not None and requested != ProvenanceFailurePolicy.INHERIT_PROJECT_POLICY:
        return requested
    if project_settings is not None:
        return project_settings.default_failure_policy
    return ENGINE_DEFAULT_FAILURE_POLICY
