"""Model-access parameter component for license/policy-gated dropdowns.

Owns the model list and decorates a node's model-selection ``Parameter`` with
an ``Options`` trait, an inline ``Button`` refresh trait, per-row entitlement
icons + subtitles, an error badge on denied selections, and runtime denial
queries. Node identity (parameter name, type, input_types, tooltip) stays with
the node so saved workflows round-trip byte-identically.

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
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.traits.button import ButtonDetailsMessagePayload

_REFRESH_ICON = "list-restart"
_DENIED_ROW_ICON = "shield-off"
_DENIED_ROW_SUBTITLE = "Not permitted by your license"
_BADGE_TITLE = "Model Not Permitted"


@dataclass
class _AccessSnapshot:
    """The cached result of one ``QueryModelAccessForNodeRequest``.

    Two lookup tables over the same set of verdicts, both keyed by the
    dropdown-name (``provider_model_id``) so a caller with a dropdown value
    can look up (a) whether it's denied and (b) what catalog id the engine
    policy matches on.

    Grouped so the "refresh replaces both atomically" contract is visible:
    ``ModelAccessComponent._fetch_snapshot()`` returns a whole new snapshot,
    which the component assigns to ``self._snapshot`` in one step. The tables
    never drift because they're never mutated in place.

    ``resolution_failure_detail`` is set (non-``None``) when the engine could
    not answer the query at all -- e.g. the node's class name isn't registered
    against a library, or the manifest declaration is missing. In that case
    both lookup tables are empty (we know NOTHING about denials or catalog
    ids), but the component must NOT treat "no denials known" as "no denials"
    at runtime. ``ModelAccessComponent.query_for_denial()`` synthesizes a
    denial from this detail so the run fails closed with a clear error, rather
    than silently letting a would-be-gated model through.
    """

    denial_by_provider_id: dict[str, CheckpointDenial] = field(default_factory=dict)
    catalog_id_by_provider_id: dict[str, str] = field(default_factory=dict)
    resolution_failure_detail: str | None = None


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
        # _AccessSnapshot's docstring for the contract.
        self._snapshot: _AccessSnapshot = self._fetch_snapshot()

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
        denial = self._snapshot.denial_by_provider_id.get(value)
        if denial is None:
            parameter.clear_badge()
            return
        parameter.set_badge(
            variant="error",
            title=_BADGE_TITLE,
            message=f"Model `{value}` is not permitted. Running this node will fail.\n\nReason(s): {denial.reason()}",
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
        # Fail-closed: if the initial snapshot couldn't resolve this node at
        # all, we can't evaluate policy for any value -- but the developer's
        # setup bug must NOT silently open the gate. Synthesize a denial.
        if self._snapshot.resolution_failure_detail is not None:
            return CheckpointDenial(failures=(CheckpointFailure(detail=self._snapshot.resolution_failure_detail),))
        catalog_id = self._snapshot.catalog_id_by_provider_id.get(value)
        if catalog_id is None:
            return None
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

    def _fetch_snapshot(self) -> _AccessSnapshot:
        """Ask the engine and build a fresh ``_AccessSnapshot`` from the response.

        On ``Success``: populate both lookup tables from the verdicts so they
        never drift. An empty verdict list is a valid response (node declares
        no gated models) and yields an empty snapshot with
        ``resolution_failure_detail=None``.

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
        snapshot = _AccessSnapshot()
        for verdict in result.verdicts:
            if verdict.provider_model_id is None:
                continue
            snapshot.catalog_id_by_provider_id[verdict.provider_model_id] = verdict.model_id
            if verdict.denial is not None:
                snapshot.denial_by_provider_id[verdict.provider_model_id] = verdict.denial
        return snapshot

    def _build_ui_options(self) -> dict[str, Any]:
        """Build the ``ui_options`` dict that decorates the dropdown row-by-row."""
        denials = self._snapshot.denial_by_provider_id
        data: list[dict[str, str]] = []
        for choice in self._model_choices:
            if choice in denials:
                data.append({"name": choice, "icon": _DENIED_ROW_ICON, "subtitle": _DENIED_ROW_SUBTITLE})
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
