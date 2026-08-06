"""Model-access parameter component for license/policy-gated dropdowns.

Owns the model list and decorates a node's model-selection ``Parameter`` with
an ``Options`` trait, an inline ``Button`` refresh trait, per-row entitlement
icons + subtitles, an error badge on denied selections, and runtime denial
queries. Node identity (parameter name, type, input_types, tooltip) stays with
the node so saved workflows round-trip byte-identically.

A dropdown stores the provider's own model id (e.g.
``"dreamina-seedance-2-0-260128"``) -- the id a node already needs in order to
build its upstream API request -- and this component resolves it to the
catalog ``model_id`` the permission layer gates on. That way a node never has
to think in catalog terms, and the mapping lives here rather than being
restated at every call site. Each row's ``ui_options["data"]`` entry carries a
``"label"``: the catalog's human-readable ``display_name`` for that choice,
rendered by the dropdown instead of the raw provider id. The label is
presentation only -- the parameter's stored value stays the provider model id,
and identity for gating is always the catalog ``model_id`` it resolves to.
``deprecated_values`` covers the values a node used to store before it adopted
this convention (an old display label, a catalog key): the mapping is accepted
wherever a value is assigned, migrated to its canonical choice, and never
offered as a fresh selection.

Usage — one construction step per parameter:

    class DescribeImage(ControlNode):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            model_param = Parameter(
                name="model",
                type="str",
                input_types=["str", "Prompt Model Config"],
                default_value=DEFAULT_MODEL,
                ...,
                # NO traits={Options(...)} -- component adds Options + Button itself.
            )
            self.add_parameter(model_param)
            self._model_access = ModelAccessComponent(
                node=self,
                parameter=model_param,
                model_choices=MODEL_CHOICES,
                default_model=DEFAULT_MODEL,
            )

The component's constructor does everything in one step: fetches the initial
snapshot, validates the parameter, adds the ``Options`` + ``Button`` traits,
sets ``ui_options`` for per-row decoration, applies the initial badge for the
current stored value, and — if the caller's ``default_value`` is denied but a
different value is currently permitted — resets the parameter's stored value
to a permitted alternative via ``set_parameter_value(..., initial_setup=True)``.
The parameter's declarative ``default_value`` is untouched.

Nodes then forward ``after_value_set`` for the model parameter to
``self._model_access.on_value_changed(value)``, and pick a failure-routing
idiom that matches their base class:

  - ControlNode / raise-based execute paths call ``raise_if_denied(value)``.
  - SuccessFailureNode / GriptapeProxyNode nodes call ``query_for_denial(value)``
    and route the reason into ``self._set_status_results(was_successful=False,
    result_details=denial.reason())``.

Nodes that reinstall the ``Options`` trait themselves (e.g. after a driver
disconnect) call ``reinstall_options()`` to put the component's trait +
decoration + badge back in place.

Composition (not inheritance) is deliberate. Three reasons:

1. **Base class diversity.** The candidate node set inherits from at least
   4 different bases -- ``ControlNode``, ``GriptapeProxyNode`` (3 levels deep
   over ``SuccessFailureNode(BaseNode)``), and config-node bases. A mixin
   would force an MRO on every consumer and collide with existing hierarchies
   (especially ``GriptapeProxyNode``'s). Composition is base-class-agnostic.
2. **Namespace hygiene.** A mixin adds ~7 public methods (``refresh``,
   ``on_value_changed``, ``query_for_denial``, ``raise_if_denied``, etc.) to
   the node's public surface. ``refresh`` in particular is a common name that
   could clash with existing node methods. Composition keeps them scoped to
   ``self._model_access.foo(...)``.
3. **Multiple instances per node.** A node with two model-selection
   parameters (a prompt model + an image model, for example) trivially holds
   two component instances. A mixin can't be instantiated twice on one class.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events.access_events import (
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.events.access_events import ModelAccessVerdict
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.traits.button import ButtonDetailsMessagePayload

_REFRESH_ICON = "list-restart"
_DENIED_ROW_ICON = "shield-off"
_DENIED_ROW_SUBTITLE = "Not permitted by your license"
_BADGE_TITLE = "Model Not Permitted"


@dataclass
class _AccessSnapshot:
    """The cached result of one ``QueryModelAccessForNodeRequest``.

    Everything is keyed on the catalog ``model_id``, which every verdict always
    carries. A dropdown choice is a ``provider_model_id`` (see the module
    docstring), so ``model_id_by_choice`` holds the resolution step: the subset
    of the offered choices whose provider id matched a verdict, mapped to that
    verdict's catalog ``model_id``.

    ``unresolved_choices`` are the offered choices that matched no declared
    catalog model. When the node declares models at all, that is an authoring
    bug rather than a policy outcome: the dropdown still renders them, but
    ``ModelAccessComponent.query_for_denial`` fails them closed rather than
    pretending the policy allowed something it never saw. A node that declares
    no models (``declares_models=False``) is not participating in model gating,
    so its choices stay permitted -- ``INVOKE_MODEL`` remains their only gate.

    Grouped so the "refresh replaces everything atomically" contract is visible:
    ``ModelAccessComponent._fetch_snapshot()`` returns a whole new snapshot,
    which the component assigns to ``self._snapshot`` in one step. The tables
    never drift because they're never mutated in place.

    ``display_name_by_model_id`` is the catalog's human-readable name for a
    model id, populated from the verdicts on the same terms as
    ``denial_by_model_id``. It exists for rendering the dropdown row and
    error/badge text only -- ``ModelAccessComponent`` never looks a value up
    BY its display name; identity is always the catalog ``model_id``.

    ``resolution_failure_detail`` is set (non-``None``) when the engine could
    not answer the query at all -- e.g. the node's class name isn't registered
    against a library, or the manifest declaration is missing. In that case
    every table is empty (we know NOTHING about denials or catalog ids), but the
    component must NOT treat "no denials known" as "no denials" at runtime.
    ``ModelAccessComponent.query_for_denial()`` synthesizes a denial from this
    detail so the run fails closed with a clear error, rather than silently
    letting a would-be-gated model through.
    """

    denial_by_model_id: dict[str, CheckpointDenial] = field(default_factory=dict)
    display_name_by_model_id: dict[str, str] = field(default_factory=dict)
    model_id_by_choice: dict[str, str] = field(default_factory=dict)
    unresolved_choices: tuple[str, ...] = ()
    declares_models: bool = False
    resolution_failure_detail: str | None = None

    def denial_for_choice(self, choice: str) -> CheckpointDenial | None:
        """The denial recorded for a dropdown choice, or ``None`` when permitted or unresolved."""
        model_id = self.model_id_by_choice.get(choice)
        if model_id is None:
            return None
        return self.denial_by_model_id.get(model_id)


class ModelAccessComponent:
    """Composition helper for a model-selection dropdown that respects license policy.

    Node constructs its Parameter (owning name / type / input_types / tooltip /
    default_value / ui_options), calls ``node.add_parameter(parameter)``, then
    passes the Parameter to this component's constructor. The constructor
    installs an ``Options`` trait with the component's ``model_choices``,
    installs a ``Button`` refresh trait, sets ``ui_options`` for per-row
    entitlement decoration, and applies the initial badge if the current
    stored value is denied. If the parameter's ``default_value`` is denied but
    a different choice is currently permitted, the constructor resets the
    stored value to that permitted default (the declarative ``default_value``
    is left unchanged).

    Runtime methods -- ``query_for_denial(value)`` / ``raise_if_denied(value)``
    -- gate the node's execute path against the current policy.
    """

    def __init__(
        self,
        *,
        node: BaseNode,
        parameter: Parameter,
        model_choices: list[str],
        default_model: str,
        deprecated_values: dict[str, str] | None = None,
    ) -> None:
        """Attach the component to an already-added Parameter and decorate it.

        Every entry in ``model_choices`` is a ``provider_model_id`` -- the
        upstream provider's own name for the model -- which the component
        resolves to the catalog model id the policy gates on.
        ``deprecated_values`` maps a historical stored value (an old display
        label, a catalog key, anything a prior version of this node once
        stored) to the current choice it becomes. Every value in the mapping
        must be one of ``model_choices``, and no key may itself already be a
        current choice; either problem raises at construction.

        A legacy value is accepted wherever it is assigned -- the ``Options``
        trait's ``choices`` include it -- but migrated to its canonical choice
        by a converter, and never offered as a fresh selection: the dropdown's
        ``ui_options["data"]`` rows come from ``model_choices`` alone. The
        parameter's current stored value is migrated the same way at
        construction, before the denied-default relocation described below.

        Choices that resolve to nothing the node declares are logged at
        construction and fail closed at run time: the node offers a model the
        catalog cannot identify, so policy cannot be evaluated for it.

        Preconditions (checked; a misuse raises rather than silently misbehaving):

        - ``parameter`` must already be attached to ``node`` (via
          ``node.add_parameter(parameter)``). Traits + badges applied to an
          unattached parameter would not emit UI events.
        - ``parameter`` must not already carry an ``Options`` or ``Button``
          trait. Adding a second ``Options`` results in an ambiguous dropdown;
          adding a second ``Button`` overloads the refresh row. Migrate the
          node to construct the parameter without those traits and let the
          component add them.
        - ``deprecated_values`` must map only to entries in ``model_choices``,
          and none of its keys may already be a current choice. Either
          violation raises.
        """
        # Constructor inputs -- immutable across the component's lifetime.
        self._node = node
        self._parameter = parameter
        self._model_choices = list(model_choices)
        self._default_model = default_model
        self._deprecated_values = dict(deprecated_values or {})

        # Fail-fast preconditions -- see docstring.
        if self._node.get_parameter_by_name(parameter.name) is not parameter:
            msg = (
                f"ModelAccessComponent: parameter '{parameter.name}' is not attached to node "
                f"'{self._node.name}'. Call node.add_parameter(parameter) BEFORE constructing "
                "the component."
            )
            raise ValueError(msg)
        if parameter.find_elements_by_type(Options):
            msg = (
                f"ModelAccessComponent: parameter '{parameter.name}' on node '{self._node.name}' "
                "already carries an Options trait. Remove traits={Options(...)} from the "
                "Parameter constructor -- ModelAccessComponent adds Options itself."
            )
            raise ValueError(msg)
        if parameter.find_elements_by_type(Button):
            msg = (
                f"ModelAccessComponent: parameter '{parameter.name}' on node '{self._node.name}' "
                "already carries a Button trait. Remove it -- ModelAccessComponent adds the "
                "refresh Button itself."
            )
            raise ValueError(msg)
        choice_set = set(self._model_choices)
        invalid_values = sorted(
            {legacy for legacy, canonical in self._deprecated_values.items() if canonical not in choice_set}
        )
        colliding_keys = sorted(legacy for legacy in self._deprecated_values if legacy in choice_set)
        if invalid_values or colliding_keys:
            problems = []
            if invalid_values:
                problems.append(f"value(s) not in model_choices: {', '.join(repr(v) for v in invalid_values)}")
            if colliding_keys:
                problems.append(f"key(s) already a current choice: {', '.join(repr(k) for k in colliding_keys)}")
            msg = (
                f"ModelAccessComponent: parameter '{parameter.name}' on node '{self._node.name}' "
                f"deprecated_values is invalid: {'; '.join(problems)}."
            )
            raise ValueError(msg)

        # Cached result of the last QueryModelAccessForNodeRequest. Replaced
        # atomically on refresh so its two lookup tables never drift. See
        # _AccessSnapshot's docstring for the contract.
        self._snapshot: _AccessSnapshot = self._fetch_snapshot()

        # Install decoration + traits. Options accepts legacy values too, so an
        # assignment carrying one is never snapped to choices[0] before the
        # migration converter (added below) gets a chance to run.
        parameter.add_trait(Options(choices=[*self._model_choices, *self._deprecated_values]))
        parameter.add_trait(
            Button(
                icon=_REFRESH_ICON,
                size="icon",
                variant="secondary",
                on_click=self._on_refresh_click,
                tooltip="Refresh available models",
            )
        )
        parameter.update_ui_options(self._build_ui_options())
        parameter.add_converter(self._convert_legacy_value)

        # Migrate a legacy stored value to its canonical choice before
        # considering whether that choice is currently permitted.
        current_value = self._node.get_parameter_value(parameter.name)
        migrated_value = self.migrate_value(current_value)
        if migrated_value is not None:
            self._node.set_parameter_value(parameter.name, migrated_value, initial_setup=True)
            current_value = migrated_value

        # If the caller's declared default_value is denied but another choice
        # IS permitted, move the parameter's stored value to that permitted
        # alternative so the artist opens the node with a usable selection.
        # The Parameter's declarative default_value is untouched -- the
        # override is a stored-value change only, via set_parameter_value
        # with initial_setup=True so no change events fire.
        if isinstance(current_value, str) and self._snapshot.denial_for_choice(current_value) is not None:
            replacement = self.pick_permitted_default()
            if replacement is not None and replacement != current_value:
                self._node.set_parameter_value(parameter.name, replacement, initial_setup=True)
                current_value = replacement

        # Apply the initial badge for whatever the (possibly-moved) current value is.
        self.on_value_changed(current_value)

    @property
    def model_choices(self) -> list[str]:
        """The component's copy of the dropdown-name list. Read-only view.

        Node code that needs the list (validation branches, connection-removal
        handlers) should read from here so the component stays the single
        source of truth for what's on offer.
        """
        return list(self._model_choices)

    def display_name_for_choice(self, choice: str) -> str:
        """The catalog's human-readable name for a dropdown choice, for a node's own messages.

        Falls back to ``choice`` itself when the catalog declares no display
        name for it, or when the choice did not resolve to a catalogued model
        at all -- so the caller always has something readable to render and
        never has to check for ``None``. For presentation only: the returned
        name is not looked up against anything, and the choice itself remains
        the value a node stores and this component gates on.
        """
        model_id = self._snapshot.model_id_by_choice.get(choice)
        if model_id is None:
            return choice
        return self._snapshot.display_name_by_model_id.get(model_id, choice)

    def reinstall_options(self) -> None:
        """Reinstall the ``Options`` trait and reapply decoration + badge.

        Nodes that remove and later re-add ``Options`` on the model parameter
        (e.g. after a driver connection is dropped) call this to put the
        component's state back. Idempotent: safe to call when ``Options`` is
        already present -- ``add_trait`` will replace the existing instance.
        """
        parameter = self._parameter
        parameter.add_trait(Options(choices=[*self._model_choices, *self._deprecated_values]))
        parameter.update_ui_options(self._build_ui_options())
        self.on_value_changed(self._node.get_parameter_value(parameter.name))

    def on_value_changed(self, value: Any) -> None:
        """Set or clear the parameter's badge based on the new value.

        Node forwards from ``after_value_set``. Cheap: local map lookup, no
        engine round-trip. A driver / Agent connection replaces the string
        value with a non-string object; that clears the badge because the
        dropdown isn't the source of truth in that state.
        """
        parameter = self._parameter
        if not isinstance(value, str):
            parameter.clear_badge()
            return
        denial = self._snapshot.denial_for_choice(value)
        if denial is None:
            parameter.clear_badge()
            return
        display_name = self.display_name_for_choice(value)
        parameter.set_badge(
            variant="error",
            title=_BADGE_TITLE,
            message=(
                f"Model `{display_name}` is not permitted. Running this node will fail.\n\nReason(s): {denial.reason()}"
            ),
            icon=_DENIED_ROW_ICON,
        )

    def refresh(self) -> None:
        """Re-query the engine and rebuild the decoration + current-selection badge.

        Called by the internal refresh button; nodes can also call it directly
        (e.g. after an external event may have changed the policy).
        """
        self._snapshot = self._fetch_snapshot()
        parameter = self._parameter
        parameter.update_ui_options(self._build_ui_options())
        self.on_value_changed(self._node.get_parameter_value(parameter.name))

    def query_for_denial(self, value: Any) -> CheckpointDenial | None:
        """Ask the engine whether ``value`` is currently permitted.

        Returns the ``CheckpointDenial`` if the model is denied, else ``None``.

        The parameter type is ``Any`` on purpose: callers pass the parameter's
        stored value straight through (``self.get_parameter_value("model")``),
        and that value can legitimately be either a ``str`` (the dropdown
        selection) OR a driver object (when a Prompt Model Config / Agent is
        connected upstream). The component only gates the ``str`` case; other
        shapes bypass the gate, because a connected driver carries its own
        model identity that the component isn't the source of truth for.

        Semantics:

        - Non-string values (driver objects, ``None``, anything else): return
          ``None``. Bypasses the gate entirely -- see paragraph above.
        - Initial snapshot resolution failed (see ``_AccessSnapshot``): return
          a **synthesized** denial with a "policy could not be evaluated"
          reason. This is the fail-closed contract -- a broken library
          registration must not silently let denied models through.
        - A value that is not one of this component's offered choices: return
          ``None``. The dropdown is not the source of truth for it -- a node that
          swaps its choices for another provider's models at run time, or holds a
          value from an upstream connection, is outside this component's remit.
        - An offered choice that resolves to none of the models the node
          declares: return a **synthesized** denial. The node declares catalog
          models yet offers a value none of them answers to, so policy cannot be
          evaluated for it; failing closed surfaces the authoring bug instead of
          hiding it. A node that declares no models at all is exempt -- it never
          opted into model gating.
        - Live engine call fails or returns no verdict for the id: return
          ``None``. These are transient conditions we don't gate user work on.

        An unresolvable choice is reported to the library author twice -- a
        warning naming it when the component installs, and this fail-closed
        denial when it runs -- but it is deliberately not badged on the
        parameter: the fix belongs to whoever wrote the manifest, and an artist
        cannot act on it.

        Use directly from SuccessFailureNode / GriptapeProxyNode::

            denial = self._model_access.query_for_denial(model)
            if denial is not None:
                self._set_status_results(was_successful=False,
                                         result_details=denial.reason())
                return ...  # per your base class's contract
        """
        # Connected driver / Agent (or None): the string dropdown isn't the
        # source of truth for the model in this state; bypass the gate.
        if not isinstance(value, str):
            return None
        unevaluable = self._unevaluable_denial(value)
        if unevaluable is not None:
            return unevaluable
        model_id = self._snapshot.model_id_by_choice.get(value)
        # A value this component never offered belongs to whatever put it there.
        if model_id is None:
            return None
        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(
                node_type=type(self._node).__name__,
                candidate_model_ids=[model_id],
            )
        )
        if not isinstance(result, QueryModelAccessForNodeResultSuccess) or not result.verdicts:
            return None
        return result.verdicts[0].denial

    def _unevaluable_denial(self, value: str) -> CheckpointDenial | None:
        """Synthesize a denial when policy cannot be evaluated for ``value`` at all.

        Two fail-closed cases, both authoring bugs rather than policy outcomes:
        the engine could not resolve this node type, or the node declares catalog
        models yet offers a choice none of them answers to. Everything else
        returns ``None`` so the caller proceeds to the live query.
        """
        if self._snapshot.resolution_failure_detail is not None:
            return CheckpointDenial(failures=(CheckpointFailure(detail=self._snapshot.resolution_failure_detail),))
        if value in self._snapshot.model_id_by_choice:
            return None
        if not self._snapshot.declares_models or value not in self._model_choices:
            return None
        detail = (
            f"License policy could not be evaluated for '{value}' on node "
            f"'{type(self._node).__name__}': it matches no model declared by this node. "
            "Verify the library manifest's model_usage block declares a model with this provider model id."
        )
        return CheckpointDenial(failures=(CheckpointFailure(detail=detail),))

    def raise_if_denied(self, value: Any) -> None:
        """Convenience wrapper: raise ``RuntimeError`` if ``query_for_denial`` returns a denial.

        Use from ControlNode / raise-based execute paths where the surrounding
        code expects a raised exception. SuccessFailureNode / GriptapeProxyNode
        subclasses should call ``query_for_denial`` directly instead so they
        can route the failure into ``_set_status_results``.
        """
        denial = self.query_for_denial(value)
        if denial is None:
            return
        display_name = self.display_name_for_choice(value)
        msg = f"Cannot run {type(self._node).__name__}: '{display_name}' is not permitted. {denial.reason()}"
        raise RuntimeError(msg)

    def pick_permitted_default(self) -> str | None:
        """Return the value the node should use as its ``default_value=``, or ``None``.

        Prefers the node's ``default_model`` when it's currently allowed. Falls
        back to the first allowed entry in ``model_choices``. Returns ``None``
        when every declared choice is currently denied.

        Called internally by ``__init__`` to move the parameter's stored value
        off a denied default. Kept public for callers that want to consult the
        permitted-default separately (e.g. logging, picking a value for a
        related parameter).
        """
        if self._is_permitted(self._default_model):
            return self._default_model
        for choice in self._model_choices:
            if self._is_permitted(choice):
                return choice
        return None

    def migrate_value(self, value: Any) -> str | None:
        """The canonical choice ``value`` migrates to if it is a deprecated key, else ``None``.

        Non-``str`` input returns ``None`` -- the same rationale as
        ``query_for_denial``: a connected driver's value isn't a dropdown token
        this component's tables cover.
        """
        if not isinstance(value, str):
            return None
        return self._deprecated_values.get(value)

    def _is_permitted(self, choice: str) -> bool:
        """The choice resolves to a catalog model and no denial was recorded for it."""
        model_id = self._snapshot.model_id_by_choice.get(choice)
        if model_id is None:
            return False
        return model_id not in self._snapshot.denial_by_model_id

    def _fetch_snapshot(self) -> _AccessSnapshot:
        """Ask the engine and build a fresh ``_AccessSnapshot`` from the response.

        On ``Success``: a dropdown choice is a ``provider_model_id``, so it is
        resolved to the catalog ``model_id`` the verdicts are keyed on. Denials
        and display names are recorded by that same catalog id, so every table
        comes from the same query and they never drift. An empty verdict list is
        a valid response (the node declares no gated models).

        The catalog schema permits two entries to share one ``provider_model_id``
        (the same upstream model with different ``key_support``). A dropdown
        value then cannot say which entry policy should apply to, so the first
        declared entry wins and the collision is logged: silently gating against
        the wrong entry would be worse than a noisy warning.

        On ``Failure`` (or any unexpected result type): log a warning naming
        the node type + the failure reason, and return a snapshot with
        ``resolution_failure_detail`` set. This distinguishes "engine says no
        denials" (fine) from "engine could not answer" (fail-closed at
        runtime). See ``_AccessSnapshot`` for the fail-closed contract.
        """
        node_type = type(self._node).__name__
        result: ResultPayload = GriptapeNodes.handle_request(QueryModelAccessForNodeRequest(node_type=node_type))
        if not isinstance(result, QueryModelAccessForNodeResultSuccess):
            details = getattr(result, "result_details", None) or type(result).__name__
            logger.warning(
                "ModelAccessComponent: engine could not resolve access for node type '%s' (%s). "
                "Dropdown decoration is empty and runtime denial checks fail closed for this node. "
                "Verify that the node's class is registered and its griptape_nodes_library.json "
                "entry declares a model_usage block.",
                node_type,
                details,
            )
            return _AccessSnapshot(
                resolution_failure_detail=(
                    f"License policy could not be evaluated for node '{node_type}' ({details}). "
                    "Verify the library manifest declares this node type with a model_usage block."
                )
            )
        model_id_by_choice = self._resolve_choices(result.verdicts, node_type=node_type)
        declares_models = bool(result.verdicts)
        unresolved = tuple(choice for choice in self._model_choices if choice not in model_id_by_choice)
        if unresolved and declares_models:
            logger.warning(
                "ModelAccessComponent: node type '%s' offers %s, which match no model it declares. "
                "Selecting one fails closed at run time. Declare the model in the library manifest so "
                "its provider_model_id matches this value.",
                node_type,
                ", ".join(repr(choice) for choice in unresolved),
            )
        elif unresolved:
            logger.warning(
                "ModelAccessComponent: node type '%s' declares no models, so its dropdown cannot be "
                "narrowed by license policy. Add a model_usage block to its griptape_nodes_library.json "
                "entry to gate these choices: %s.",
                node_type,
                ", ".join(repr(choice) for choice in unresolved),
            )

        snapshot = _AccessSnapshot(
            model_id_by_choice=model_id_by_choice,
            unresolved_choices=unresolved,
            declares_models=declares_models,
        )
        for verdict in result.verdicts:
            if verdict.denial is not None:
                snapshot.denial_by_model_id[verdict.model_id] = verdict.denial
            if verdict.display_name is not None:
                snapshot.display_name_by_model_id[verdict.model_id] = verdict.display_name
        return snapshot

    def _resolve_choices(self, verdicts: Sequence[ModelAccessVerdict], *, node_type: str) -> dict[str, str]:
        """Map each offered choice to the catalog ``model_id`` that license policy gates on.

        A choice is a ``provider_model_id`` (see the module docstring), so the
        verdicts are inverted into ``provider_model_id -> model_id`` and the
        offered choices are selected from that. A verdict carrying no provider
        id contributes nothing: it describes a catalog entry no dropdown can
        name. Choices absent from the result are left out entirely, which is
        what makes them ``unresolved_choices`` and fails them closed at run time.

        The catalog permits two entries to share one ``provider_model_id``, in
        which case the first declared entry claims it and the collision is
        logged for any choice actually affected. Gating against an arbitrary one
        of two entries is not something to do silently.
        """
        model_id_by_provider_id: dict[str, str] = {}
        colliding_provider_ids: set[str] = set()
        for verdict in verdicts:
            provider_model_id = verdict.provider_model_id
            if provider_model_id is None:
                continue
            claimed_model_id = model_id_by_provider_id.get(provider_model_id)
            if claimed_model_id is None:
                model_id_by_provider_id[provider_model_id] = verdict.model_id
                continue
            if claimed_model_id != verdict.model_id:
                colliding_provider_ids.add(provider_model_id)
        model_id_by_choice = {
            choice: model_id_by_provider_id[choice]
            for choice in self._model_choices
            if choice in model_id_by_provider_id
        }
        offered_collisions = sorted(
            provider_id for provider_id in colliding_provider_ids if provider_id in model_id_by_choice
        )
        if offered_collisions:
            logger.warning(
                "ModelAccessComponent: node type '%s' declares several catalog models that share the "
                "provider model id(s) %s, so a dropdown value cannot identify which entry license "
                "policy applies to. Gating uses the first declared entry. Offer only one of the "
                "colliding catalog entries, or give them distinct provider model ids.",
                node_type,
                ", ".join(repr(provider_id) for provider_id in offered_collisions),
            )
        return model_id_by_choice

    def _build_ui_options(self) -> dict[str, Any]:
        """Build the ``ui_options`` dict that decorates the dropdown row-by-row.

        Built from ``model_choices`` alone, never ``deprecated_values`` -- a
        legacy value is accepted when assigned but never offered as a fresh
        selection. Each row's ``"label"`` is the catalog display name for that
        choice (``display_name_for_choice`` falls back to the raw choice), so a
        row never renders blank even when the catalog has nothing to say about it.
        """
        data: list[dict[str, str]] = []
        for choice in self._model_choices:
            row: dict[str, str] = {"name": choice, "label": self.display_name_for_choice(choice)}
            if self._snapshot.denial_for_choice(choice) is not None:
                row["icon"] = _DENIED_ROW_ICON
                row["subtitle"] = _DENIED_ROW_SUBTITLE
            data.append(row)
        return {
            "data": data,
            "dropdown_row_icons": True,
            "dropdown_row_subtitles": True,
        }

    def _convert_legacy_value(self, value: Any) -> Any:
        """Migrate a legacy stored value to its canonical choice; everything else passes through.

        Installed as a directly-attached converter (``Parameter.add_converter``)
        rather than hooked into ``before_value_set`` / ``after_value_set``,
        because ``BaseNode.set_parameter_value`` runs every converter
        unconditionally on every assignment path -- including workflow load,
        which calls it with ``initial_setup=True`` and
        ``skip_before_value_set=True`` and therefore never calls
        ``before_value_set`` at all. A converter is the only hook a saved
        workflow's stored value is guaranteed to pass through, so it is the
        only place this migration can live.
        """
        migrated = self.migrate_value(value)
        if migrated is not None:
            return migrated
        return value

    def _on_refresh_click(
        self,
        _button: Button,
        _button_details: ButtonDetailsMessagePayload,
    ) -> None:
        """Handler for the inline refresh button. Delegates to ``refresh()``."""
        self.refresh()
