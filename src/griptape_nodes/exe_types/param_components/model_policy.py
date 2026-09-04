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
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from griptape_nodes.retained_mode.managers.event_manager import reentrant_bus_in_init_would_report

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.retained_mode.engine import Engine

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
    only what policy denied; ``catalog_ids_by_provider_id`` maps every resolved handle to the stable
    catalog keys policy gates on.

    That key is deliberately NOT unique: ``Model``'s contract allows two catalog entries to describe
    the same ``provider_model_id`` with different ``key_support`` (e.g. a BYOK entry and a
    hosted-key entry). So a denial on ANY entry sharing a handle denies the handle, and
    ``catalog_ids_for`` returns every catalog id behind it rather than whichever was seen last --
    otherwise the permitted twin of a denied entry would let the denied one run. That is also why
    there is exactly one catalog table rather than a handle-to-single-id map beside it: a second
    table holding "whichever entry was seen first" would be the shape this one exists to replace,
    and keeping both invites an edit that updates one and not the other.

    ``failure_detail`` is set when the engine could not answer at all (unregistered node class,
    missing manifest declaration). Both tables are then empty, and a caller must not read "no
    denials known" as "no denials" -- see ``denial_for``.

    ``has_unmatchable_entries`` is True when a resolved model declared no ``provider_model_id``.
    Such an entry is declared but cannot be matched against a dropdown value, which makes
    ``catalog_ids_by_provider_id`` an incomplete view of the catalog. Callers that would refuse an
    unrecognized value must not do so in that case; absence proves nothing.

    ``unmatchable_denials`` names the catalog ids that policy DENIED but that carry no
    ``provider_model_id`` to match a dropdown value against. Those denials cannot be honored
    per-row, so they are honored for the whole parameter instead -- see ``denial_for``. Dropping
    them would let an explicitly forbidden model run.

    ``deferred`` is True when the query was skipped entirely because issuing it would have
    tripped the reentrant-bus-in-init strict-mode rule -- a node ``__init__`` on the stack
    *inside* a strict-mode scope, which in practice means the worker's schema probe or node
    execution (see ``reentrant_bus_in_init_would_report``). Both tables are empty and every
    lookup answers "no denial" -- a deferred snapshot must not read as fail-closed, because no
    query has failed; one was never made. It is replaced wholesale by the next
    ``query_model_policy()``, which for a probed node type is the first refresh after
    construction.
    """

    denial_by_provider_id: dict[str, CheckpointDenial] = field(default_factory=dict)
    # Every catalog id behind a shared provider_model_id, for callers that re-ask policy live.
    catalog_ids_by_provider_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    failure_detail: str | None = None
    has_unmatchable_entries: bool = False
    unmatchable_denials: tuple[str, ...] = ()
    deferred: bool = False

    def catalog_ids_for(self, provider_model_id: str) -> tuple[str, ...]:
        """Every catalog id declared against ``provider_model_id``, in declaration order."""
        return self.catalog_ids_by_provider_id.get(provider_model_id, ())

    @property
    def declares_models(self) -> bool:
        """Whether the node declared any model at all.

        Keyed on the raw verdict count rather than on the lookup tables: a node whose declared
        models all lack a ``provider_model_id`` still HAS a catalog, and reading that as "declares
        nothing" would silently disable enforcement for it.
        """
        return bool(self.catalog_ids_by_provider_id) or self.has_unmatchable_entries

    def denial_for(  # noqa: PLR0911 -- a chain of early-exit verdicts, one per snapshot state
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
        # "Not a model" is decided before any refusal, including the fail-closed one. Every real
        # handle still fails closed below -- a declared repo id never reduces to `None` -- but a
        # placeholder row badged "Model Not Permitted" would report a library-registration problem
        # as a licensing one, and hide the "download this model" message that says what to do.
        if provider_model_id is None:
            return None
        # A deferred snapshot has asked policy nothing, so it can deny nothing. Without this,
        # the `refuse_unrecognized` path below would refuse every choice against the empty
        # catalog -- badging a freshly constructed gated dropdown entirely "not permitted".
        if self.deferred:
            return None
        if self.failure_detail is not None:
            return CheckpointDenial(failures=(CheckpointFailure(detail=self.failure_detail),))
        denial = self.denial_by_provider_id.get(provider_model_id)
        if denial is not None:
            return denial
        # A denial we cannot attribute to a row still has to be honored. Refusing the whole
        # parameter over-blocks, but the alternative is running a model policy explicitly forbade,
        # and `has_unmatchable_entries` has already switched off the undeclared backstop that would
        # otherwise have caught it.
        if self.unmatchable_denials:
            # Artist-facing: they cannot edit a library manifest, so state the effect and who to
            # ask. The manifest instruction goes to the log in `query_model_policy` instead.
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(
                        detail=(
                            "Your license does not permit one of the models this node offers, and this "
                            "library does not describe its models precisely enough to tell which one. No "
                            "model can be used here until the library is updated. Contact whoever "
                            "maintains this node library."
                        )
                    ),
                )
            )
        is_unrecognized = provider_model_id not in self.catalog_ids_by_provider_id
        if refuse_unrecognized and not self.has_unmatchable_entries and is_unrecognized:
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(
                        detail=(
                            f"'{provider_model_id}' is not one of the models this node library declares, so "
                            "your license cannot be checked against it. Pick one of the listed models, or ask "
                            "whoever maintains this node library to add it."
                        )
                    ),
                )
            )
        return None


# Shared "policy not yet queried" snapshot. Frozen, so one instance serves every deferral.
DEFERRED_SNAPSHOT = ModelPolicySnapshot(deferred=True)


def query_model_policy(engine: Engine, node_type: str, *, fail_closed: bool = True) -> ModelPolicySnapshot:
    """Ask the engine which of ``node_type``'s declared models are permitted.

    Returns ``DEFERRED_SNAPSHOT`` without querying when the request would trip
    reentrant-bus-in-init: a node ``__init__`` on the stack inside a strict-mode scope, which
    is the worker's schema probe (where the violation drops the class from the worker schema)
    or node execution. Those callers hold the deferred snapshot until their first
    post-construction refresh replaces it with a real query result.

    Construction OUTSIDE such a scope -- an editor drop, a workflow load, any single-process
    engine, where nothing observes the violation and no probe exists -- queries normally, so
    the dropdown's denial rows and badge are correct as soon as the node appears rather than
    only after it runs.

    Args:
        engine: Engine to ask about model access.
        node_type: The node class name the manifest declares ``model_usage`` against.
        fail_closed: What an unanswerable query means. When True, the returned snapshot carries a
            ``failure_detail`` so every subsequent lookup denies -- a broken library registration
            must not silently open the gate. When False, the failure is treated as "this library
            has not adopted declarations", which is the pre-adoption status quo rather than an
            error, and the snapshot is empty.
    """
    if reentrant_bus_in_init_would_report():
        logger.debug(
            "Deferring model-policy query for node type '%s': node __init__ in progress under a strict-mode scope.",
            node_type,
        )
        return DEFERRED_SNAPSHOT
    result = engine.handle_request(QueryModelAccessForNodeRequest(node_type=node_type))
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
        # Artist-facing, like the `unmatchable_denials` wording in `denial_for`: state the effect
        # and who to ask. The node type, the engine's reason, and the manifest instruction stay in
        # the warning above -- an artist cannot edit a library manifest, and naming one reads as a
        # licensing problem when the actual fault is a broken registration.
        return ModelPolicySnapshot(
            failure_detail=(
                "This node's models could not be checked against your license, so nothing can be "
                "used here yet. Contact whoever maintains this node library."
            )
        )

    denials: dict[str, CheckpointDenial] = {}
    all_catalog_ids: dict[str, list[str]] = {}
    unmatchable = False
    unmatchable_denials: list[str] = []
    for verdict in result.verdicts:
        # `provider_model_id` is optional on a catalog `Model`, and per ModelAccessVerdict's
        # contract its absence means "declared, but with no upstream handle" -- NOT "unresolved".
        if verdict.provider_model_id is None:
            unmatchable = True
            if verdict.denial is not None:
                unmatchable_denials.append(verdict.model_id)
            continue
        all_catalog_ids.setdefault(verdict.provider_model_id, []).append(verdict.model_id)
        # Any-denial-wins: two entries can share this handle, and the permitted one must not
        # overwrite the denied one.
        if verdict.denial is not None:
            denials[verdict.provider_model_id] = verdict.denial

    if unmatchable_denials:
        logger.warning(
            "Node type '%s' declares model(s) %s that license policy DENIES, but they carry no "
            "provider_model_id, so the denial cannot be matched to a dropdown row. Refusing the whole "
            "parameter instead. Add provider_model_id to those catalog entries.",
            node_type,
            unmatchable_denials,
        )

    return ModelPolicySnapshot(
        denial_by_provider_id=denials,
        catalog_ids_by_provider_id={k: tuple(v) for k, v in all_catalog_ids.items()},
        has_unmatchable_entries=unmatchable,
        unmatchable_denials=tuple(unmatchable_denials),
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
