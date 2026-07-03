"""Model-access parameter component for license/policy-gated dropdowns.

Owns the model list and decorates a node's model-selection ``Parameter`` with
an ``Options`` trait, an inline ``Button`` refresh trait, per-row entitlement
icons + subtitles, an error badge on denied selections, and runtime denial
queries. Node identity (parameter name, type, input_types, tooltip) stays with
the node so saved workflows round-trip byte-identically.

Composition pattern (like ``huggingface_model_parameter.HuggingFaceModelParameter``):

    class DescribeImage(ControlNode):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._model_access = ModelAccessParameter(
                node=self,
                model_choices=MODEL_CHOICES,
                default_model=DEFAULT_MODEL,
            )
            model_param = Parameter(
                name="model",
                type="str",
                input_types=["str", "Prompt Model Config"],
                default_value=self._model_access.pick_permitted_default() or DEFAULT_MODEL,
                ...,
                # NO traits={Options(...)} -- helper installs Options + Button
            )
            self.add_parameter(model_param)
            self._model_access.install(model_param)

Nodes then forward ``after_value_set`` for the model parameter to
``self._model_access.on_value_changed(value)``, and pick a failure-routing
idiom that matches their base class:

  - ControlNode / raise-based execute paths call ``raise_if_denied(value)``.
  - SuccessFailureNode / GriptapeProxyNode nodes call ``query_for_denial(value)``
    and route the reason into ``self._set_status_results(was_successful=False,
    result_details=denial.reason())``.

Nodes that reinstall the ``Options`` trait themselves (e.g. after a driver
disconnect) call ``reinstall_options()`` to put the helper's trait + decoration
+ badge back in place.
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
    ``ModelAccessParameter._fetch_snapshot()`` returns a whole new snapshot,
    which the helper assigns to ``self._snapshot`` in one step. The tables
    never drift because they're never mutated in place.

    ``resolution_failure_detail`` is set (non-``None``) when the engine could
    not answer the query at all -- e.g. the node's class name isn't registered
    against a library, or the manifest declaration is missing. In that case
    both lookup tables are empty (we know NOTHING about denials or catalog
    ids), but the helper must NOT treat "no denials known" as "no denials" at
    runtime. ``ModelAccessParameter.query_for_denial()`` synthesizes a denial
    from this detail so the run fails closed with a clear error, rather than
    silently letting a would-be-gated model through.
    """

    denial_by_provider_id: dict[str, CheckpointDenial] = field(default_factory=dict)
    catalog_id_by_provider_id: dict[str, str] = field(default_factory=dict)
    resolution_failure_detail: str | None = None


class ModelAccessParameter:
    """Composition helper for a model-selection dropdown that respects license policy.

    Attaches to a Parameter the node has already added, via ``install(parameter)``.
    Owns the model list, installs the ``Options`` and ``Button`` traits, applies
    per-row decoration + badge, and exposes ``query_for_denial()`` /
    ``raise_if_denied()`` for the node's execute path.
    """

    def __init__(
        self,
        *,
        node: BaseNode,
        model_choices: list[str],
        default_model: str,
    ) -> None:
        # Constructor inputs -- immutable across the helper's lifetime.
        self._node = node
        self._model_choices = list(model_choices)
        self._default_model = default_model
        # Set by install(). Held so we don't call get_parameter_by_name on every
        # operation. The reference is stable across the node's lifetime;
        # removing the parameter from the node would be a node-side lifecycle
        # decision we don't try to detect here.
        self._parameter: Parameter | None = None
        # Cached result of the last QueryModelAccessForNodeRequest. Replaced
        # atomically on refresh so its two lookup tables never drift. See
        # _AccessSnapshot's docstring for the contract.
        self._snapshot: _AccessSnapshot = self._fetch_snapshot()

    @property
    def model_choices(self) -> list[str]:
        """The helper's copy of the dropdown-name list. Read-only view.

        Node code that needs the list (validation branches, connection-removal
        handlers) should read from here so the helper stays the single source
        of truth for what's on offer.
        """
        return list(self._model_choices)

    def install(self, parameter: Parameter) -> None:
        """Attach to the given parameter and install its access-related pieces.

        Adds an ``Options`` trait with the helper's ``model_choices``, a
        ``Button`` refresh trait, sets ``ui_options`` with per-row decoration,
        and applies the initial badge if the parameter's current value is
        denied.

        Preconditions (checked; a misuse raises rather than silently misbehaving):

        - ``parameter`` must already be attached to the node (via
          ``node.add_parameter(parameter)``). Traits + badges applied to an
          unattached parameter would not emit UI events.
        - ``install()`` must not have been called before. Re-installing on a
          different parameter would leave the first one with orphaned traits.
        - The parameter must not already carry an ``Options`` or ``Button``
          trait. Adding a second ``Options`` results in an ambiguous dropdown;
          adding a second ``Button`` overloads the refresh row. Migrate the
          node to construct the parameter without those traits and let
          ``install()`` add them.
        """
        # Fail-fast preconditions -- see docstring.
        if self._parameter is not None:
            msg = (
                f"ModelAccessParameter.install() was already called for parameter "
                f"'{self._parameter.name}' on node '{self._node.name}'. "
                "Call install() exactly once."
            )
            raise RuntimeError(msg)
        if self._node.get_parameter_by_name(parameter.name) is not parameter:
            msg = (
                f"ModelAccessParameter.install() received a Parameter ('{parameter.name}') "
                f"that is not attached to node '{self._node.name}'. "
                "Call node.add_parameter(parameter) BEFORE install(parameter)."
            )
            raise ValueError(msg)
        if parameter.find_elements_by_type(Options):
            msg = (
                f"ModelAccessParameter.install(): parameter '{parameter.name}' on node "
                f"'{self._node.name}' already carries an Options trait. Remove traits="
                "{Options(...)} from the Parameter constructor -- install() adds Options itself."
            )
            raise ValueError(msg)
        if parameter.find_elements_by_type(Button):
            msg = (
                f"ModelAccessParameter.install(): parameter '{parameter.name}' on node "
                f"'{self._node.name}' already carries a Button trait. Remove it -- "
                "install() adds the refresh Button itself."
            )
            raise ValueError(msg)

        self._parameter = parameter
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
        self.on_value_changed(self._node.get_parameter_value(parameter.name))

    def reinstall_options(self) -> None:
        """Reinstall the ``Options`` trait and reapply decoration + badge.

        Nodes that remove and later re-add ``Options`` on the model parameter
        (e.g. after a driver connection is dropped) call this to put the
        helper's state back. Idempotent: safe to call when ``Options`` is
        already present -- ``add_trait`` will replace the existing instance.
        """
        parameter = self._parameter
        if parameter is None:
            return
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
        if parameter is None:
            return
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
        if parameter is not None:
            parameter.update_ui_options(self._build_ui_options())
            self.on_value_changed(self._node.get_parameter_value(parameter.name))

    def query_for_denial(self, value: Any) -> CheckpointDenial | None:
        """Ask the engine whether ``value`` is currently permitted.

        Returns the ``CheckpointDenial`` if the model is denied, else ``None``.

        The parameter type is ``Any`` on purpose: callers pass the parameter's
        stored value straight through (``self.get_parameter_value("model")``),
        and that value can legitimately be either a ``str`` (the dropdown
        selection) OR a driver object (when a Prompt Model Config / Agent is
        connected upstream). The helper only gates the ``str`` case; other
        shapes bypass the gate, because a connected driver carries its own
        model identity that the helper isn't the source of truth for.

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
        when every declared choice is currently denied -- the caller decides
        what to do in that case (typically: fall back to the node's own
        ``DEFAULT_MODEL`` so the parameter has a value the badge can render
        against; the runtime gate will still deny on run).
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
                "ModelAccessParameter: engine could not resolve access for node type '%s' (%s). "
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
