"""Shared license-policy layer for model-selection parameters.

Two components put model dropdowns on nodes: ``ModelAccessComponent`` decorates a *static*
dropdown whose choices a library author enumerated, and ``HuggingFaceModelParameter`` builds its
choices by scanning the local HuggingFace cache. They differ entirely in how they own the
``Parameter`` -- traits, ``ui_options`` keys, refresh timing -- and deliberately do not compose.

They do not differ in what "is this model permitted?" means. That question is this module: query
the policy layer once, hold the verdicts in an immutable snapshot, and answer lookups from it. Both
components delegate here so a policy change lands in one place and the two surfaces cannot drift
into giving opposite answers for the same model.

What stays with each component: installing traits, writing ``ui_options``, deciding when to
refresh, and choosing how a denial reaches the artist (row icon, badge, raised error).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.access_events import (
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter

logger = logging.getLogger("griptape_nodes")

# Denial decoration, shared so a gated static dropdown and a gated HuggingFace dropdown are
# indistinguishable to an artist.
DENIED_ROW_ICON = "shield-off"
DENIED_ROW_SUBTITLE = "Not permitted by your license"
BADGE_TITLE = "Model Not Permitted"


@dataclass(frozen=True)
class ModelPolicySnapshot:
    """The result of one ``QueryModelAccessForNodeRequest``, as an immutable unit.

    Frozen and replaced wholesale by ``query_model_policy()``, so the tables cannot drift apart:
    there is no window where denials describe one query and declared ids another.

    Both tables are keyed by ``provider_model_id`` -- the upstream provider's name for the model --
    because that is the handle a dropdown value can be reduced to. ``denial_by_provider_id`` holds
    only what policy denied; ``catalog_id_by_provider_id`` maps every resolved entry to the stable
    catalog key policy gates on.

    ``failure_detail`` is set when the engine could not answer at all (unregistered node class,
    missing manifest declaration). Both tables are then empty, and a caller must not read "no
    denials known" as "no denials" -- see ``denial_for``.

    ``has_unmatchable_entries`` is True when a resolved model declared no ``provider_model_id``.
    Such an entry is declared but cannot be matched against a dropdown value, which makes
    ``catalog_id_by_provider_id`` an incomplete view of the catalog. Callers that would refuse an
    unrecognized value must not do so in that case; absence proves nothing.
    """

    denial_by_provider_id: dict[str, CheckpointDenial] = field(default_factory=dict)
    catalog_id_by_provider_id: dict[str, str] = field(default_factory=dict)
    failure_detail: str | None = None
    has_unmatchable_entries: bool = False

    @property
    def declares_models(self) -> bool:
        """Whether the node declared any model at all.

        Keyed on the raw verdict count rather than on the lookup tables: a node whose declared
        models all lack a ``provider_model_id`` still HAS a catalog, and reading that as "declares
        nothing" would silently disable enforcement for it.
        """
        return bool(self.catalog_id_by_provider_id) or self.has_unmatchable_entries

    def denial_for(
        self, provider_model_id: str | None, *, refuse_unrecognized: bool = False
    ) -> CheckpointDenial | None:
        """Return the denial for a resolved dropdown value, or ``None`` when permitted.

        Args:
            provider_model_id: The value reduced to its provider handle, or ``None`` when the value
                is not a model at all (a placeholder row, a connected driver object). ``None`` is
                never denied.
            refuse_unrecognized: Whether a value absent from the catalog should be refused. Off by
                default, matching a static dropdown whose choices were all vetted at authoring
                time. Callers whose choices come from an untrusted source (a local cache scan) turn
                it on so an undeclared model cannot pass by omission -- but only when
                ``has_unmatchable_entries`` is False, since otherwise absence is uninformative.
        """
        if self.failure_detail is not None:
            return CheckpointDenial(failures=(CheckpointFailure(detail=self.failure_detail),))
        if provider_model_id is None:
            return None
        denial = self.denial_by_provider_id.get(provider_model_id)
        if denial is not None:
            return denial
        is_unrecognized = provider_model_id not in self.catalog_id_by_provider_id
        if refuse_unrecognized and not self.has_unmatchable_entries and is_unrecognized:
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(
                        detail=(
                            f"Model '{provider_model_id}' is not declared in this library's model catalog, so "
                            "license policy cannot be evaluated for it. Add it to the catalog to make it "
                            "selectable."
                        )
                    ),
                )
            )
        return None


def query_model_policy(node_type: str, *, fail_closed: bool = True) -> ModelPolicySnapshot:
    """Ask the engine which of ``node_type``'s declared models are permitted.

    Args:
        node_type: The node class name the manifest declares ``model_usage`` against.
        fail_closed: What an unanswerable query means. When True, the returned snapshot carries a
            ``failure_detail`` so every subsequent lookup denies -- a broken library registration
            must not silently open the gate. When False, the failure is treated as "this library
            has not adopted declarations", which is the pre-adoption status quo rather than an
            error, and the snapshot is empty.
    """
    result = GriptapeNodes.handle_request(QueryModelAccessForNodeRequest(node_type=node_type))
    if not isinstance(result, QueryModelAccessForNodeResultSuccess):
        details = getattr(result, "result_details", None) or type(result).__name__
        if not fail_closed:
            logger.debug("Model policy unavailable for node type '%s' (%s); not enforcing.", node_type, details)
            return ModelPolicySnapshot()
        logger.warning(
            "Could not resolve model access for node type '%s' (%s). Selections will be refused until this "
            "resolves. Verify the node's griptape_nodes_library.json entry declares a model_usage block.",
            node_type,
            details,
        )
        return ModelPolicySnapshot(
            failure_detail=(
                f"License policy could not be evaluated for node '{node_type}' ({details}). "
                "Verify the library manifest declares this node type with a model_usage block."
            )
        )

    denials: dict[str, CheckpointDenial] = {}
    catalog_ids: dict[str, str] = {}
    unmatchable = False
    for verdict in result.verdicts:
        # `provider_model_id` is optional on a catalog `Model`, and per ModelAccessVerdict's
        # contract its absence means "declared, but with no upstream handle" -- NOT "unresolved".
        if verdict.provider_model_id is None:
            unmatchable = True
            continue
        catalog_ids[verdict.provider_model_id] = verdict.model_id
        if verdict.denial is not None:
            denials[verdict.provider_model_id] = verdict.denial

    return ModelPolicySnapshot(
        denial_by_provider_id=denials,
        catalog_id_by_provider_id=catalog_ids,
        has_unmatchable_entries=unmatchable,
    )


def apply_denial_badge(parameter: Parameter, value: str, denial: CheckpointDenial | None) -> None:
    """Set or clear ``parameter``'s denial badge.

    Always clears when there is no denial, so a badge cannot outlive the condition that set it
    (a license change, or enforcement being turned off entirely).
    """
    if denial is None:
        parameter.clear_badge()
        return
    parameter.set_badge(
        variant="error",
        title=BADGE_TITLE,
        message=f"Model `{value}` is not permitted. Running this node will fail.\n\nReason(s): {denial.reason()}",
        icon=DENIED_ROW_ICON,
    )
