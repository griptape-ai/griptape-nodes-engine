"""Shared license-policy layer for model-selection parameters.

Two components put model dropdowns on nodes: ``ModelAccessComponent`` decorates a *static*
dropdown whose choices a library author enumerated, and ``HuggingFaceModelParameter`` builds its
choices by scanning the local HuggingFace cache. They differ entirely in how they own the
``Parameter`` -- traits, ``ui_options`` keys, refresh timing -- and deliberately do not compose.

They do not differ in what "is this model permitted?" means. That question is this module: query
the policy layer once, hold the verdicts in an immutable snapshot, and answer lookups from it. Both
components delegate here so a policy change lands in one place and the two surfaces cannot drift
into giving opposite answers for the same model.

Every node-attributed query is built by ``node_access_request``, including the live per-value
re-asks a component makes at run time. Asking about a node means naming both the node type and the
library it came from -- see that function -- and one construction point is what keeps the second
half from being forgotten.

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
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


@dataclass(frozen=True)
class DenialDecoration:
    """How a refusal reads to an artist: the row it marks and the badge it raises.

    Shared by both dropdown components, so a gated static dropdown and a gated HuggingFace
    dropdown are indistinguishable.

    There are two instances, and which one a parameter wears is the difference between an answer
    and a missing answer. ``DENIED_DECORATION`` is for a check that ran and said no: the artist's
    license does not cover the model, and nothing about that is a fault. ``CHECK_FAILED_DECORATION``
    is for a check that could not run at all, which is the engine's fault and not theirs. Wearing
    the denied icon and "Not permitted by your license" for that state sent artists to compare
    pricing tiers over what was really a broken registration, so the two states say different
    things on every surface: the row, the badge title, and the badge's opening line.

    ``badge_lead`` is that opening line and carries a ``{value}`` placeholder for the model id;
    ``apply_denial_badge`` fills it in and appends the consequence and the reason.
    """

    icon: str
    row_subtitle: str
    badge_title: str
    badge_lead: str


DENIED_DECORATION = DenialDecoration(
    icon="shield-off",
    row_subtitle="Not permitted by your license",
    badge_title="Model Not Permitted",
    badge_lead="Model `{value}` is not permitted.",
)

CHECK_FAILED_DECORATION = DenialDecoration(
    # `alert-triangle` is what `ParameterMessage` maps its "warning" variant to; an unanswerable
    # check is that, not a verdict.
    icon="alert-triangle",
    row_subtitle="Couldn't be checked",
    badge_title="Model Check Failed",
    badge_lead="Griptape Nodes couldn't check whether `{value}` is permitted.",
)


@dataclass(frozen=True)
class ModelPolicySnapshot:
    """The result of one ``QueryModelAccessForNodeRequest``, as an immutable unit.

    Frozen and replaced wholesale by ``query_model_policy()``, so the tables cannot drift apart:
    there is no window where denials describe one query and declared ids another.

    All three tables are keyed by ``provider_model_id`` -- the upstream provider's name for the
    model -- because that is the handle a dropdown value can be reduced to.
    ``denial_by_provider_id`` holds only what policy denied; ``catalog_ids_by_provider_id`` maps
    every resolved handle to the stable catalog keys policy gates on;
    ``display_name_by_provider_id`` maps it to the catalog's readable name, for decorating a row a
    person reads.

    That key is deliberately NOT unique: ``Model``'s contract allows two catalog entries to describe
    the same ``provider_model_id`` with different ``key_support`` (e.g. a BYOK entry and a
    hosted-key entry). So a denial on ANY entry sharing a handle denies the handle, and
    ``catalog_ids_for`` returns every catalog id behind it rather than whichever was seen last --
    otherwise the permitted twin of a denied entry would let the denied one run. That is also why
    there is exactly one catalog table rather than a handle-to-single-id map beside it: a second
    table holding "whichever entry was seen first" would be the shape this one exists to replace,
    and keeping both invites an edit that updates one and not the other.

    ``display_name_by_provider_id`` is a handle-to-single-value map, which is the shape the
    paragraph above rejects, and it is safe only because a name decides nothing. It keeps the first
    name declared for a shared handle: there is no any-wins rule to inherit, because a name is not a
    verdict, and picking the "wrong" twin's name changes how a row reads rather than what may run.
    A name must never be used as an identity -- what a dropdown stores and what a payload sends is
    the handle itself.

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
    display_name_by_provider_id: dict[str, str] = field(default_factory=dict)
    failure_detail: str | None = None
    has_unmatchable_entries: bool = False
    unmatchable_denials: tuple[str, ...] = ()
    deferred: bool = False

    def catalog_ids_for(self, provider_model_id: str) -> tuple[str, ...]:
        """Every catalog id declared against ``provider_model_id``, in declaration order."""
        return self.catalog_ids_by_provider_id.get(provider_model_id, ())

    def display_name_for(self, provider_model_id: str) -> str | None:
        """The catalog's readable name for ``provider_model_id``, or ``None`` if it has none.

        ``None`` means the catalog does not describe this handle, which is a legitimate state for a
        choice the catalog never declared. Callers render the handle itself in that case; they must
        not synthesize a name.
        """
        return self.display_name_by_provider_id.get(provider_model_id)

    @property
    def declares_models(self) -> bool:
        """Whether the node declared any model at all.

        Keyed on the raw verdict count rather than on the lookup tables: a node whose declared
        models all lack a ``provider_model_id`` still HAS a catalog, and reading that as "declares
        nothing" would silently disable enforcement for it.
        """
        return bool(self.catalog_ids_by_provider_id) or self.has_unmatchable_entries

    @property
    def decoration(self) -> DenialDecoration:
        """How this snapshot's refusals should read on a row and in a badge.

        The one place the two states are told apart, so a surface cannot be missed and left
        announcing a licensing problem for an engine-side fault.

        Only ``failure_detail`` -- a query the engine could not answer -- reads as "couldn't be
        checked". The ``unmatchable_denials`` refusal in ``denial_for`` deliberately does NOT:
        policy really did deny a model there, and telling an artist their license was never
        consulted would be false.
        """
        if self.failure_detail is not None:
            return CHECK_FAILED_DECORATION
        return DENIED_DECORATION

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
                            "model can be used here until the library is updated. If this node came with "
                            "Griptape Nodes, please report it from the editor's File > Report Issue menu; "
                            "otherwise, contact whoever maintains this node library."
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


def node_access_request(node: BaseNode, candidate_model_ids: list[str] | None = None) -> QueryModelAccessForNodeRequest:
    """Build the node-attributed access query for ``node``, naming the library it came from.

    Both fields come off ``node.metadata``, where ``Library.create_node`` recorded them, because
    neither is reliably derivable from the class. A class name is not unique across libraries --
    two installed libraries may each register ``Flux2ImageGeneration`` -- and a library keys its
    node types by the name its JSON declared, which ``register_lazy_node_type`` never compares to
    ``__name__``. So a query built from ``type(node).__name__`` can resolve to no library or to no
    type at all, and an unresolved query fails closed: every model on the node is denied, which
    reads to an artist as a licensing problem when the check never ran. ``get_declared_models``,
    which fills the same dropdown's choices, reads the same two fields.

    A node built outside the library path -- a transient probe, a test fixture -- recorded neither,
    so the type falls back to ``type(node).__name__`` and the library to ``None``, leaving the
    engine's lookup-by-name that is correct whenever exactly one library declares the type.

    Args:
        node: The node the query is attributed to. Supplies both the node type and the library.
        candidate_model_ids: Narrow the query to these catalog ids. ``None`` (default) lets the
            engine derive the candidates from the node's declarations.
    """
    library_name = node.metadata.get("library")
    if not isinstance(library_name, str):
        library_name = None
    node_type = node.metadata.get("node_type")
    if not isinstance(node_type, str):
        node_type = type(node).__name__
    return QueryModelAccessForNodeRequest(
        node_type=node_type,
        specific_library_name=library_name,
        candidate_model_ids=candidate_model_ids,
    )


def query_model_policy(node: BaseNode, *, fail_closed: bool = True) -> ModelPolicySnapshot:
    """Ask the engine which of ``node``'s declared models are permitted.

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
        node: The node whose declared models to check. Supplies the engine to ask, plus the
            registered node type and library that ``node_access_request`` derives the query from.
        fail_closed: What an unanswerable query means. When True, the returned snapshot carries a
            ``failure_detail`` so every subsequent lookup denies -- a broken library registration
            must not silently open the gate. When False, the failure is treated as "this library
            has not adopted declarations", which is the pre-adoption status quo rather than an
            error, and the snapshot is empty.
    """
    request = node_access_request(node)
    if reentrant_bus_in_init_would_report():
        logger.debug(
            "Deferring model-policy query for node type '%s': node __init__ in progress under a strict-mode scope.",
            request.node_type,
        )
        return DEFERRED_SNAPSHOT
    result = node.engine.handle_request(request)
    if not isinstance(result, QueryModelAccessForNodeResultSuccess):
        details = getattr(result, "result_details", None) or type(result).__name__
        if not fail_closed:
            logger.debug("Model policy unavailable for node type '%s' (%s); not enforcing.", request.node_type, details)
            return ModelPolicySnapshot()
        logger.warning(
            "Could not resolve model access for node type '%s' (%s). Selections will be refused until this "
            "resolves. Verify the node's griptape_nodes_library.json entry declares a model_usage block.",
            request.node_type,
            details,
        )
        # Artist-facing, like the `unmatchable_denials` wording in `denial_for`: state the effect
        # and who to tell. The node type, the engine's reason, and the manifest instruction stay in
        # the warning above -- an artist cannot edit a library manifest, and naming one reads as a
        # licensing problem when the actual fault is a broken registration.
        #
        # Which is why the message says outright that this is a bug and names where to report it.
        # This path fires on a resolution fault the artist had no part in and cannot fix, and the
        # library it would have named is usually one of ours; pointing them at a maintainer, in
        # wording built around their license, sent them comparing plan tiers for an engine bug.
        # The menu path stays useful when this string arrives as a run error in a headless `gtn
        # run` or a published workflow, because it says where the menu is rather than assuming
        # they are looking at it.
        return ModelPolicySnapshot(
            failure_detail=(
                "Griptape Nodes couldn't check which models this node is allowed to use, so nothing "
                "can be used here yet. This is a bug, not a limit on your plan or your API key. "
                "Please report it from the editor's File > Report Issue menu."
            )
        )

    denials: dict[str, CheckpointDenial] = {}
    all_catalog_ids: dict[str, list[str]] = {}
    display_names: dict[str, str] = {}
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
        # First-name-wins for a shared handle, per ModelPolicySnapshot's contract: a name is not a
        # verdict, so there is nothing here to fail closed on.
        if verdict.display_name is not None:
            display_names.setdefault(verdict.provider_model_id, verdict.display_name)
        # Any-denial-wins: two entries can share this handle, and the permitted one must not
        # overwrite the denied one.
        if verdict.denial is not None:
            denials[verdict.provider_model_id] = verdict.denial

    if unmatchable_denials:
        logger.warning(
            "Node type '%s' declares model(s) %s that license policy DENIES, but they carry no "
            "provider_model_id, so the denial cannot be matched to a dropdown row. Refusing the whole "
            "parameter instead. Add provider_model_id to those catalog entries.",
            request.node_type,
            unmatchable_denials,
        )

    return ModelPolicySnapshot(
        denial_by_provider_id=denials,
        catalog_ids_by_provider_id={k: tuple(v) for k, v in all_catalog_ids.items()},
        display_name_by_provider_id=display_names,
        has_unmatchable_entries=unmatchable,
        unmatchable_denials=tuple(unmatchable_denials),
    )


def apply_denial_badge(
    parameter: Parameter, value: str, denial: CheckpointDenial | None, *, decoration: DenialDecoration
) -> None:
    """Set or clear ``parameter``'s denial badge.

    Always clears when there is no denial, so a badge cannot outlive the condition that set it
    (a license change, or enforcement being turned off entirely).

    ``decoration`` is required rather than defaulted, so a caller cannot quietly raise a badge
    saying "not permitted by your license" over a check that never ran. Pass the owning snapshot's
    ``decoration``.
    """
    if denial is None:
        parameter.clear_badge()
        return
    lead = decoration.badge_lead.format(value=value)
    parameter.set_badge(
        variant="error",
        title=decoration.badge_title,
        message=f"{lead} Running this node will fail.\n\nReason(s): {denial.reason()}",
        icon=decoration.icon,
    )
