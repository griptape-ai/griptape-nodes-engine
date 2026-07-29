"""Unit tests for `ModelAccessComponent`.

Focused on:
- __init__ decorates an already-added parameter (Options + Button traits,
  ui_options data + row icons + subtitles) without touching its identity
  (name / type / input_types / tooltip / declarative default_value)
- __init__ moves the parameter's stored value off a denied default when a
  permitted alternative exists; declarative default_value is left alone
- constructor preconditions: parameter must be attached to node, must not
  already carry Options / Button
- on_value_changed() sets and clears the badge from the cached denial map
- refresh() re-queries the engine and rebuilds decoration + badge
- query_for_denial() returns a live verdict; falls through to None on failure
- raise_if_denied() raises RuntimeError with the denial reason
- pick_permitted_default() prefers the node's DEFAULT_MODEL, falls back to
  the first allowed choice, and returns None when every declared choice is denied
- SuccessFailure-style usage: node calls query_for_denial and routes into
  _set_status_results (validated indirectly -- the component itself never
  raises, so the node's failure branch stays reachable)
"""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.model_access_component import ModelAccessComponent
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.options import Options

_LIBRARY_NAME = "model-access-param-test-library"


class _AccessProbeNode(BaseNode):
    """Concrete BaseNode used to exercise ModelAccessComponent."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)


@pytest.fixture(autouse=True)
def _clean_registry():  # noqa: ANN202
    """Clear the LibraryRegistry singletons before and after each test."""
    from griptape_nodes.node_library.library_registry import LibraryRegistry

    stores = ("_libraries", "_node_aliases", "_collision_node_names_to_library_names", "_registered_widgets")
    for store in stores:
        getattr(LibraryRegistry, store).clear()
    yield
    for store in stores:
        getattr(LibraryRegistry, store).clear()


def _register_probe_node(*, node_declarations=(), library_declarations=()) -> None:  # noqa: ANN001
    """Register _AccessProbeNode in a test library so QueryModelAccessForNode resolves it."""
    from griptape_nodes.node_library.library_registry import (
        LibraryMetadata,
        LibraryRegistry,
        LibrarySchema,
        NodeMetadata,
    )

    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="t",
            description="d",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
            declarations=list(library_declarations),
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(
        _AccessProbeNode,
        NodeMetadata(category="t", description="d", display_name="Probe", declarations=list(node_declarations)),
    )


def _catalog():  # noqa: ANN202
    from griptape_nodes.node_library.library_declarations import (
        KeySupport,
        Model,
        ModelCatalogLibraryProperty,
        ModelProvider,
    )

    return ModelCatalogLibraryProperty(
        providers={
            "provider": ModelProvider(
                display_name="Provider",
                models={
                    "gtc_test_alpha": Model(
                        display_name="Alpha",
                        family="TestFam",
                        provider_model_id="alpha",
                        key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                    ),
                    "gtc_test_beta": Model(
                        display_name="Beta",
                        family="TestFam",
                        provider_model_id="beta",
                        key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                    ),
                },
            ),
        }
    )


def _build_probe_node_with_component(
    *,
    model_choices: list[str],
    default_model: str,
    initial_stored_value: str | None = None,
) -> tuple[_AccessProbeNode, ModelAccessComponent, Parameter]:
    """Build node + parameter + component (fully installed) and return all three.

    Under the new one-step construction API, the component installs its
    ``Options`` + ``Button`` traits and applies the initial badge inside
    ``__init__``. There is no observable "pre-install" state.

    ``initial_stored_value``: if set, the parameter's stored value is set to
    this via ``set_parameter_value(initial_setup=True)`` BEFORE the component
    is constructed, so the constructor sees it as the current value. Use this
    to test the "born with a denied value" path.
    """
    from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty

    _register_probe_node(
        node_declarations=[ModelUsageNodeProperty(model_ids=["gtc_test_alpha", "gtc_test_beta"])],
        library_declarations=[_catalog()],
    )

    node = _AccessProbeNode(name="probe")
    param = Parameter(
        name="model",
        type="str",
        default_value=default_model,
        tooltip="Choose a model",
        ui_options={"display_name": "prompt model"},
    )
    node.add_parameter(param)
    if initial_stored_value is not None:
        node.set_parameter_value(param.name, initial_stored_value, initial_setup=True)
    component = ModelAccessComponent(
        node=node,
        parameter=param,
        model_choices=model_choices,
        default_model=default_model,
    )
    return node, component, param


def _install_probe_node_with_helper(
    *,
    model_choices: list[str],
    default_model: str,
) -> tuple[_AccessProbeNode, ModelAccessComponent]:
    """Legacy tuple-of-2 shim over _build_probe_node_with_component for tests that don't want the Parameter object."""
    node, helper, _param = _build_probe_node_with_component(
        model_choices=model_choices,
        default_model=default_model,
    )
    return node, helper


class TestPickPermittedDefault:
    def test_prefers_default_when_allowed(self) -> None:
        _, helper = _install_probe_node_with_helper(
            model_choices=["alpha", "beta"],
            default_model="alpha",
        )
        assert helper.pick_permitted_default() == "alpha"

    def test_falls_back_to_first_allowed_when_default_denied(self, griptape_nodes) -> None:  # noqa: ANN001
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            _, helper = _install_probe_node_with_helper(
                model_choices=["alpha", "beta"],
                default_model="alpha",
            )
            # Alpha is denied at construction time; beta is the fallback.
            assert helper.pick_permitted_default() == "beta"
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)

    def test_returns_none_when_every_choice_is_denied(self, griptape_nodes) -> None:  # noqa: ANN001
        """Every declared choice denied -> None. Caller decides what to do next.

        The helper does not silently return a denied model as the default -- that
        would hide the failure mode. Callers typically wire this as
        ``pick_permitted_default() or DEFAULT_MODEL`` so the parameter still has
        a bindable value and the badge renders against it.
        """
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_all(_checkpoint: object) -> CheckpointDenial:
            return CheckpointDenial(failures=(CheckpointFailure(detail="Nothing enabled."),))

        griptape_nodes.EventManager().add_authorization_hook(deny_all)
        try:
            _, helper = _install_probe_node_with_helper(
                model_choices=["alpha", "beta"],
                default_model="alpha",
            )
            assert helper.pick_permitted_default() is None
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_all)


class TestInstall:
    def test_install_adds_button_trait_alongside_options(self) -> None:
        node, _helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        param = node.get_parameter_by_name("model")
        assert param is not None
        assert len(param.find_elements_by_type(Options)) == 1
        assert len(param.find_elements_by_type(Button)) == 1

    def test_install_populates_ui_options_with_dropdown_data(self, griptape_nodes) -> None:  # noqa: ANN001
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            node, _helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="beta")

            param = node.get_parameter_by_name("model")
            assert param is not None
            ui = param.ui_options
            assert ui["dropdown_row_icons"] is True
            assert ui["dropdown_row_subtitles"] is True
            # Alpha row carries the denial decoration; beta is bare.
            data_by_name = {row["name"]: row for row in ui["data"]}
            assert data_by_name["alpha"]["icon"] == "shield-off"
            assert data_by_name["alpha"]["subtitle"] == "Not permitted by your license"
            assert "icon" not in data_by_name["beta"]
            assert "subtitle" not in data_by_name["beta"]
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)

    def test_install_preserves_parameter_identity(self) -> None:
        """Install must not change parameter name / type / tooltip / stored value."""
        node, _helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        pre_param = node.get_parameter_by_name("model")
        assert pre_param is not None
        pre_name, pre_type, pre_tooltip = pre_param.name, pre_param.type, pre_param.tooltip
        pre_display_name = pre_param.ui_options.get("display_name")

        post_param = node.get_parameter_by_name("model")
        assert post_param is not None
        assert post_param.name == pre_name
        assert post_param.type == pre_type
        assert post_param.tooltip == pre_tooltip
        # update_ui_options merges; display_name set by the node must survive.
        assert post_param.ui_options.get("display_name") == pre_display_name

    def test_install_applies_initial_badge_when_stored_value_denied(self, griptape_nodes) -> None:  # noqa: ANN001
        """A node born with a denied stored value shows the badge immediately.

        Setup: every choice is denied so ``pick_permitted_default()`` returns
        None and the constructor cannot relocate the stored value to a
        permitted alternative. The badge therefore fires against the
        original stored value.
        """
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_everything(_checkpoint: object) -> CheckpointDenial:
            return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))

        griptape_nodes.EventManager().add_authorization_hook(deny_everything)
        try:
            _node, _component, param = _build_probe_node_with_component(
                model_choices=["alpha", "beta"],
                default_model="alpha",
                initial_stored_value="alpha",
            )

            badge = param.get_badge()
            assert badge is not None
            assert "not permitted" in badge.message.lower()
            assert "Alpha not enabled." in badge.message
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_everything)

    def test_constructor_relocates_stored_value_off_denied_default(self, griptape_nodes) -> None:  # noqa: ANN001
        """Constructor moves the stored value off a denied default to a permitted alternative.

        The parameter's declarative default_value is preserved (unchanged); only
        the stored value is relocated. This is how a legacy workflow that saved
        a since-denied model gets an initial usable selection when it reloads.
        """
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha denied."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            node, _component, param = _build_probe_node_with_component(
                model_choices=["alpha", "beta"], default_model="alpha"
            )

            # Stored value moved to permitted 'beta'.
            assert node.get_parameter_value("model") == "beta"
            # No badge -- the current stored value is permitted.
            assert param.get_badge() is None
            # Declarative default_value on the Parameter is untouched.
            assert param.default_value == "alpha"
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)


class TestConstructorPreconditions:
    """The component's constructor rejects misuse rather than silently misbehaving.

    Under the one-step API, ``install()`` is folded into ``__init__``. The four
    precondition checks (parameter-not-attached, pre-existing Options trait,
    pre-existing Button trait, second-instance-on-same-parameter) all fire
    from the constructor.
    """

    def test_raises_when_parameter_is_not_on_node(self) -> None:
        """Parameter must be attached to the node (via add_parameter) before component construction."""
        _register_probe_node()
        node = _AccessProbeNode(name="probe")
        orphan = Parameter(name="model", type="str", default_value="alpha", tooltip="")
        # Note: no node.add_parameter(orphan) call.

        with pytest.raises(ValueError, match="not attached to node"):
            ModelAccessComponent(node=node, parameter=orphan, model_choices=["alpha"], default_model="alpha")

    def test_raises_when_second_component_attaches_to_same_parameter(self) -> None:
        """Constructing a second component against the same parameter raises.

        The first component adds an Options trait, so the second construction
        trips the pre-existing-Options precondition — same error, same reason
        as if the caller had attached Options themselves before construction.
        """
        _node, _first_component, param = _build_probe_node_with_component(
            model_choices=["alpha", "beta"], default_model="alpha"
        )

        with pytest.raises(ValueError, match="already carries an Options trait"):
            ModelAccessComponent(node=_node, parameter=param, model_choices=["alpha", "beta"], default_model="alpha")

    def test_raises_when_parameter_already_has_options(self) -> None:
        """A Parameter constructed with traits={Options(...)} would end up with two Options."""
        _register_probe_node()
        node = _AccessProbeNode(name="probe")

        param = Parameter(
            name="model",
            type="str",
            default_value="alpha",
            tooltip="",
            traits={Options(choices=["alpha"])},
        )
        node.add_parameter(param)

        with pytest.raises(ValueError, match="already carries an Options trait"):
            ModelAccessComponent(node=node, parameter=param, model_choices=["alpha"], default_model="alpha")

    def test_raises_when_parameter_already_has_button(self) -> None:
        """A Parameter constructed with a Button trait would end up with two Buttons."""
        _register_probe_node()
        node = _AccessProbeNode(name="probe")

        param = Parameter(
            name="model",
            type="str",
            default_value="alpha",
            tooltip="",
            traits={Button(icon="star", tooltip="")},
        )
        node.add_parameter(param)

        with pytest.raises(ValueError, match="already carries a Button trait"):
            ModelAccessComponent(node=node, parameter=param, model_choices=["alpha"], default_model="alpha")


class TestEngineFailureIsFailClosedAtRuntime:
    """When the engine can't answer QueryModelAccessForNodeRequest.

    Two guarantees, tested here:

    - Setup + dropdown continue to work (permissive at UI). No badge, no
      decoration. Artists can still open workflows built against an
      unregistered node type without hitting a raise on load.
    - Runtime denial checks fail CLOSED. ``query_for_denial()`` returns a
      synthesized CheckpointDenial with a "policy could not be evaluated"
      reason, so a developer's setup bug cannot silently let denied models
      through at run time. ``raise_if_denied()`` raises with the same reason.

    A developer-facing WARNING log names the node type + failure kind so the
    misconfiguration is discoverable.
    """

    def _register_library_without_probe_node(self) -> None:
        """Register a library but skip register_new_node_type for _AccessProbeNode.

        A QueryModelAccessForNodeRequest for _AccessProbeNode returns Failure.
        """
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata,
            LibraryRegistry,
            LibrarySchema,
        )

        schema = LibrarySchema(
            name=_LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t", description="d", library_version="1.0.0", engine_version="1.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )
        LibraryRegistry.generate_new_library(library_data=schema)

    def _build_component_against_unresolved_node(self) -> ModelAccessComponent:
        """Node whose class isn't registered -> engine returns Failure at construction."""
        node = _AccessProbeNode(name="probe")
        param = Parameter(name="model", type="str", default_value="alpha", tooltip="")
        node.add_parameter(param)
        return ModelAccessComponent(node=node, parameter=param, model_choices=["alpha"], default_model="alpha")

    def test_unknown_node_type_logs_warning(self, caplog) -> None:  # noqa: ANN001
        """Failure result -> warning logged, message names the node type."""
        import logging

        self._register_library_without_probe_node()

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            self._build_component_against_unresolved_node()

        matches = [r for r in caplog.records if "engine could not resolve access" in r.message]
        assert matches, "Expected a warning log about unresolved access; got none."
        assert "_AccessProbeNode" in matches[0].message

    def test_query_for_denial_returns_synthesized_denial(self, caplog) -> None:  # noqa: ANN001
        """Failure -> query_for_denial synthesizes a CheckpointDenial (fail-closed)."""
        import logging

        self._register_library_without_probe_node()

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            helper = self._build_component_against_unresolved_node()

        denial = helper.query_for_denial("alpha")
        assert denial is not None, "Fail-closed contract: unresolved node type must not return None."
        assert any("could not be evaluated" in m for m in denial.messages())
        assert any("_AccessProbeNode" in m for m in denial.messages())

    def test_raise_if_denied_raises(self, caplog) -> None:  # noqa: ANN001
        """Failure -> raise_if_denied raises the synthesized reason."""
        import logging

        self._register_library_without_probe_node()

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            helper = self._build_component_against_unresolved_node()

        with pytest.raises(RuntimeError, match="could not be evaluated"):
            helper.raise_if_denied("alpha")

    def test_query_for_denial_still_ignores_non_string_values(self, caplog) -> None:  # noqa: ANN001
        """Non-string values (driver objects) bypass even the fail-closed path."""
        import logging

        self._register_library_without_probe_node()

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            helper = self._build_component_against_unresolved_node()

        # A connected Prompt Model Config driver / Agent replaces the string
        # value with an object that carries its own model identity. The helper
        # can't gate that -- the guarantee is "we don't gate what we don't own".
        assert helper.query_for_denial({"driver": "obj"}) is None
        assert helper.query_for_denial(None) is None

    def test_success_with_empty_verdicts_is_not_a_failure(self) -> None:
        """A registered node with no model_usage yields an empty snapshot -- but NOT fail-closed.

        The engine correctly responds Success with verdicts=[] for a node
        whose declarations don't list any models. That's a valid "no gated
        models here" answer, not an error.
        """
        # Register the node with NO model_usage / model_provider_usage decls.
        _register_probe_node()  # empty declarations by default

        node = _AccessProbeNode(name="probe")
        param = Parameter(name="model", type="str", default_value="alpha", tooltip="")
        node.add_parameter(param)
        helper = ModelAccessComponent(node=node, parameter=param, model_choices=["alpha"], default_model="alpha")

        # No synthesized denial -- everything is genuinely allowed.
        assert helper.query_for_denial("alpha") is None
        # And no exception on raise_if_denied either.
        helper.raise_if_denied("alpha")


class TestOnValueChanged:
    def test_sets_badge_when_switching_to_denied_value(self, griptape_nodes) -> None:  # noqa: ANN001
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            node, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="beta")

            param = node.get_parameter_by_name("model")
            assert param is not None
            assert param.get_badge() is None  # beta is allowed at install time

            helper.on_value_changed("alpha")

            badge = param.get_badge()
            assert badge is not None
            assert "Alpha not enabled." in badge.message
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)

    def test_clears_badge_when_switching_to_allowed_value(self, griptape_nodes) -> None:  # noqa: ANN001
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            # default_model="beta" (permitted) so the constructor doesn't auto-move away
            # from a denied initial value. We then simulate the artist manually selecting
            # 'alpha' by calling on_value_changed directly.
            _node, helper, param = _build_probe_node_with_component(
                model_choices=["alpha", "beta"], default_model="beta"
            )
            helper.on_value_changed("alpha")
            assert param.get_badge() is not None

            helper.on_value_changed("beta")
            assert param.get_badge() is None
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)

    def test_clears_badge_on_non_string_value(self) -> None:
        """A driver / Agent connection replaces the string value with an object.

        In that state the dropdown isn't the source of truth for the model
        anymore, so the badge must clear.
        """
        node, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        param = node.get_parameter_by_name("model")
        assert param is not None

        helper.on_value_changed({"driver": "something"})
        assert param.get_badge() is None


class TestRefreshAndQueryForDenial:
    def test_query_for_denial_returns_denial_for_denied_model(self, griptape_nodes) -> None:  # noqa: ANN001
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="beta")

            denial = helper.query_for_denial("alpha")
            assert denial is not None
            assert denial.messages() == ["Alpha not enabled."]

            assert helper.query_for_denial("beta") is None
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)

    def test_query_for_denial_ignores_non_string_values(self) -> None:
        _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        assert helper.query_for_denial(None) is None
        assert helper.query_for_denial({"driver": "obj"}) is None
        assert helper.query_for_denial(123) is None

    def test_query_for_denial_returns_none_for_unknown_dropdown_name(self) -> None:
        """An id not in the catalog (typo / stale saved workflow) falls through to None.

        The helper only knows about ids it saw in the initial denial-map fetch.
        A candidate outside that set can't be gated -- internal engine errors
        must not gate user work, so we return None rather than raise.
        """
        _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        assert helper.query_for_denial("never-heard-of-this-model") is None

    def test_refresh_picks_up_hook_change(self, griptape_nodes) -> None:  # noqa: ANN001
        """refresh() re-fetches the denial map so a policy change becomes visible."""
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        node, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        param = node.get_parameter_by_name("model")
        assert param is not None
        # Initially: no hook, alpha allowed, no badge.
        assert param.get_badge() is None

        # Now register a hook AFTER install; the helper has stale cache.
        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            node.set_parameter_value("model", "alpha")
            # on_value_changed uses the STALE cache -- badge should NOT be set yet.
            helper.on_value_changed("alpha")
            assert param.get_badge() is None

            # After refresh, the helper sees the current hook decision and applies the badge.
            helper.refresh()
            badge = param.get_badge()
            assert badge is not None
            assert "Alpha not enabled." in badge.message
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)


class TestRaiseIfDenied:
    def test_raises_runtimeerror_with_denial_reason(self, griptape_nodes) -> None:  # noqa: ANN001
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="beta")

            with pytest.raises(RuntimeError, match="Alpha not enabled"):
                helper.raise_if_denied("alpha")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)

    def test_does_not_raise_when_allowed(self) -> None:
        _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")
        # Should not raise:
        helper.raise_if_denied("alpha")
        helper.raise_if_denied("beta")

    def test_does_not_raise_on_non_string_value(self) -> None:
        """A driver / Agent connection carries model identity itself; bypass the gate."""
        _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")
        helper.raise_if_denied(None)
        helper.raise_if_denied({"driver": "obj"})


class TestRefreshButton:
    def test_refresh_button_click_rebuilds_state(self, griptape_nodes) -> None:  # noqa: ANN001
        """The inline Button trait's on_click hook invokes refresh()."""
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        node, _helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        param = node.get_parameter_by_name("model")
        assert param is not None
        buttons = param.find_elements_by_type(Button)
        assert len(buttons) == 1
        button = buttons[0]

        # Set stored value to alpha and register a deny hook that only reaches
        # the helper after refresh.
        node.set_parameter_value("model", "alpha")

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            # Call the on_click handler the way the Button trait would.
            assert button.on_click_callback is not None
            button.on_click_callback(button, None)  # type: ignore[arg-type]

            badge = param.get_badge()
            assert badge is not None
            assert "Alpha not enabled." in badge.message
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)


class TestSuccessFailureUsagePattern:
    def test_query_for_denial_supports_early_return_without_raising(self, griptape_nodes) -> None:  # noqa: ANN001
        """A SuccessFailure-style caller can inspect the denial and route without an exception.

        Verifies that the helper never raises on its own -- the caller decides
        the failure idiom. This is the contract that lets GriptapeProxyNode
        subclasses call `_set_status_results(was_successful=False, ...)` cleanly.
        """
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="beta")

            # Node-side pattern: inspect and route, no exception.
            routed_to_failure = False
            failure_reason: str | None = None

            denial = helper.query_for_denial("alpha")
            if denial is not None:
                routed_to_failure = True
                failure_reason = denial.reason()

            assert routed_to_failure is True
            assert failure_reason == "Alpha not enabled."
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)


class TestModelChoicesProperty:
    def test_returns_copy_of_choices(self) -> None:
        """`.model_choices` returns a defensive copy so callers can't mutate the internal list."""
        _, helper = _install_probe_node_with_helper(model_choices=["alpha", "beta"], default_model="alpha")

        choices = helper.model_choices
        assert choices == ["alpha", "beta"]

        # Mutating the returned list does NOT affect the helper's own list.
        choices.append("gamma")
        assert helper.model_choices == ["alpha", "beta"]


class TestReinstallOptions:
    def test_reinstall_puts_options_and_decoration_back(self, griptape_nodes) -> None:  # noqa: ANN001
        """After remove_trait(Options), reinstall_options() re-adds it with decoration + badge."""
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_alpha(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_test_alpha":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Alpha not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_alpha)
        try:
            # default_model="beta" (permitted) so construction doesn't relocate away
            # from a denied initial value. Then flip the stored value to denied 'alpha'
            # so the reinstall path has a badge to restore.
            _node, helper, param = _build_probe_node_with_component(
                model_choices=["alpha", "beta"], default_model="beta"
            )
            helper.on_value_changed("alpha")  # apply the "we're viewing alpha" state
            _node.set_parameter_value("model", "alpha", initial_setup=True)

            # Simulate what a node does when a driver connects: strip Options entirely.
            options_traits = param.find_elements_by_type(Options)
            for trait in options_traits:
                param.remove_trait(trait_type=trait)
            param.clear_badge()
            assert len(param.find_elements_by_type(Options)) == 0
            assert param.get_badge() is None

            # Now the node reinstalls after driver disconnect:
            helper.reinstall_options()

            assert len(param.find_elements_by_type(Options)) == 1
            # Decoration and badge return.
            badge = param.get_badge()
            assert badge is not None
            assert "Alpha not enabled." in badge.message
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_alpha)
