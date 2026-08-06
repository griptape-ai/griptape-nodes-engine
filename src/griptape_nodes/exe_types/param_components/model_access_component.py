"""Model-access parameter component for license/policy-gated dropdowns.

Owns the model list and decorates a node's model-selection ``Parameter`` with
an ``Options`` trait, an inline ``Button`` refresh trait, per-row entitlement
icons + subtitles, an error badge on denied selections, and runtime denial
queries. Node identity (parameter name, type, input_types, tooltip) stays with
the node so saved workflows round-trip byte-identically.

Policy itself lives in ``model_policy``, shared with ``HuggingFaceModelParameter``
(which builds its choices by scanning the local HuggingFace cache instead of from
a declared list). The two components own their ``Parameter`` differently and do
not compose, but they must not disagree about whether a model is permitted, so
the snapshot, the query, and the denial wording are common. This component
enumerates its choices at authoring time, so it leaves
``refuse_unrecognized`` off: an id absent from the catalog is an already-vetted
value rather than something to gate user work on.

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
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.param_components.model_policy import (
    DENIED_ROW_ICON,
    DENIED_ROW_SUBTITLE,
    ModelPolicySnapshot,
    apply_denial_badge,
    query_model_policy,
)
from griptape_nodes.retained_mode.events.access_events import (
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial
    from griptape_nodes.traits.button import ButtonDetailsMessagePayload

_REFRESH_ICON = "list-restart"


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
    ) -> None:
        """Attach the component to an already-added Parameter and decorate it.

        Preconditions (checked; a misuse raises rather than silently misbehaving):

        - ``parameter`` must already be attached to ``node`` (via
          ``node.add_parameter(parameter)``). Traits + badges applied to an
          unattached parameter would not emit UI events.
        - ``parameter`` must not already carry an ``Options`` or ``Button``
          trait. Adding a second ``Options`` results in an ambiguous dropdown;
          adding a second ``Button`` overloads the refresh row. Migrate the
          node to construct the parameter without those traits and let the
          component add them.
        """
        # Constructor inputs -- immutable across the component's lifetime.
        self._node = node
        self._parameter = parameter
        self._model_choices = list(model_choices)
        self._default_model = default_model

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

        # Cached result of the last QueryModelAccessForNodeRequest. Replaced
        # atomically on refresh so its two lookup tables never drift. See
        # ModelPolicySnapshot's docstring for the contract.
        self._snapshot: ModelPolicySnapshot = self._fetch_snapshot()

        # Install decoration + traits.
        parameter.add_trait(Options(choices=list(self._model_choices)))
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

        # If the caller's declared default_value is denied but another choice
        # IS permitted, move the parameter's stored value to that permitted
        # alternative so the artist opens the node with a usable selection.
        # The Parameter's declarative default_value is untouched -- the
        # override is a stored-value change only, via set_parameter_value
        # with initial_setup=True so no change events fire.
        current_value = self._node.get_parameter_value(parameter.name)
        if isinstance(current_value, str) and current_value in self._snapshot.denial_by_provider_id:
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

    def reinstall_options(self) -> None:
        """Reinstall the ``Options`` trait and reapply decoration + badge.

        Nodes that remove and later re-add ``Options`` on the model parameter
        (e.g. after a driver connection is dropped) call this to put the
        component's state back. Idempotent: safe to call when ``Options`` is
        already present -- ``add_trait`` will replace the existing instance.
        """
        parameter = self._parameter
        parameter.add_trait(Options(choices=list(self._model_choices)))
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
        apply_denial_badge(parameter, value, self._snapshot.denial_by_provider_id.get(value))

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
        - Initial snapshot resolution failed (see ``ModelPolicySnapshot``): return
          a **synthesized** denial with a "policy could not be evaluated"
          reason. This is the fail-closed contract -- a broken library
          registration must not silently let denied models through.
        - Live engine call fails or returns no verdict for the id: return
          ``None``. These are transient conditions or already-vetted ids not
          in the catalog; we don't gate user work on them.

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
        # Fail-closed when the node could not be resolved at all: the developer's
        # setup bug must NOT silently open the gate.
        if self._snapshot.failure_detail is not None:
            return self._snapshot.denial_for(value)
        catalog_id = self._snapshot.catalog_id_by_provider_id.get(value)
        if catalog_id is None:
            # Not in the catalog. `refuse_unrecognized` stays off here: these choices were
            # enumerated by the library author, so an unrecognized id is an already-vetted
            # value rather than something to gate user work on.
            return None
        # Re-ask live for the resolved id rather than trusting the cached verdict, so a license
        # change since the last refresh is honored at run time in BOTH directions -- a newly
        # granted permission unblocks the artist without waiting for a refresh, and a newly
        # revoked one still denies. Returning a cached denial early would make grants invisible.
        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(
                node_type=type(self._node).__name__,
                candidate_model_ids=[catalog_id],
            )
        )
        if not isinstance(result, QueryModelAccessForNodeResultSuccess) or not result.verdicts:
            return None
        return result.verdicts[0].denial

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
        msg = f"Cannot run {type(self._node).__name__}: '{value}' is not permitted. {denial.reason()}"
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
        denials = self._snapshot.denial_by_provider_id
        if self._default_model not in denials:
            return self._default_model
        for choice in self._model_choices:
            if choice not in denials:
                return choice
        return None

    def _fetch_snapshot(self) -> ModelPolicySnapshot:
        """Ask the engine for this node type's policy verdicts.

        Fails closed: an unanswerable query yields a snapshot carrying
        ``failure_detail``, so runtime checks deny rather than reading "no
        denials known" as "no denials". A node that simply declares no models
        is a valid empty snapshot, not a failure.
        """
        return query_model_policy(type(self._node).__name__)

    def _build_ui_options(self) -> dict[str, Any]:
        """Build the ``ui_options`` dict that decorates the dropdown row-by-row."""
        denials = self._snapshot.denial_by_provider_id
        data: list[dict[str, str]] = []
        for choice in self._model_choices:
            if choice in denials:
                data.append({"name": choice, "icon": DENIED_ROW_ICON, "subtitle": DENIED_ROW_SUBTITLE})
            else:
                data.append({"name": choice})
        return {
            "data": data,
            "dropdown_row_icons": True,
            "dropdown_row_subtitles": True,
        }

    def _on_refresh_click(
        self,
        _button: Button,
        _button_details: ButtonDetailsMessagePayload,
    ) -> None:
        """Handler for the inline refresh button. Delegates to ``refresh()``."""
        self.refresh()
