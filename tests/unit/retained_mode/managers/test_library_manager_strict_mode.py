"""Probe-level tests for strict-mode routing in _serialize_library_node_schemas.

Uses a fixture probe detector that calls ``STRICT_MODE.report`` from inside a
node class's ``__init__``. The scope wrapper on the probe loop is then
responsible for excluding violating classes from the returned schema list.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE
from griptape_nodes.exe_types.core_types import Parameter, Trait
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import current_engine
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager


@pytest.fixture
def patched_registry() -> Callable[[dict[str, type]], AbstractContextManager[None]]:
    """Yield a context-manager factory that patches LibraryRegistry for a probe map.

    Both test classes need the same MagicMock-backed library plus the
    same ``create_node`` side-effect that constructs an instance from
    the probe map. Returning a factory keeps the per-test ``nodes``
    parameter readable at the call site.
    """

    @contextmanager
    def _patch(nodes: dict[str, type]) -> Iterator[None]:
        lib = MagicMock()
        lib.get_registered_nodes.return_value = list(nodes.keys())
        lib.get_node_class.side_effect = lambda name: nodes[name]

        def _create_node(*, node_type: str, name: str, specific_library_name: str | None = None) -> Any:  # noqa: ARG001
            # The real create_node sets the constructing-node flag; the probe's
            # detectors (and the construction-time deferrals they motivated) key
            # off it, so the mock must set it too to reproduce probe conditions.
            with LibraryRegistry.constructing_node():
                return nodes[node_type](name)

        with patch.multiple(
            "griptape_nodes.retained_mode.managers.library_manager.LibraryRegistry",
            get_library=MagicMock(return_value=lib),
            create_node=MagicMock(side_effect=_create_node),
        ):
            yield

    return _patch


class _CleanProbe:
    """Node class whose __init__ does nothing interesting."""

    parameters: list = []  # noqa: RUF012

    def __init__(self, name: str) -> None:
        self.name = name


class _ViolatingProbe:
    """Node class whose __init__ triggers a schema-dropping violation.

    Uses ``reentrant-bus-in-init`` because it is registered with
    ``drops_class_from_schema=True``; the LOAD_PROBE gate uses that flag
    to decide whether to drop the class from the schema. The rule is an
    ergonomics rule (``correctness=False``), so on the orchestrator probe
    (``is_worker=False``) it logs at WARNING while the class is still
    dropped.
    """

    parameters: list = []  # noqa: RUF012

    def __init__(self, name: str) -> None:
        self.name = name
        STRICT_MODE.report(
            rule_id="reentrant-bus-in-init",
            message="fixture probe violation",
        )


class TestSerializeSchemasStrictMode:
    @pytest.mark.asyncio
    async def test_clean_class_is_included(self, patched_registry: Callable[[dict[str, type]], Any]) -> None:
        manager = current_engine().library_manager
        with patched_registry({"Clean": _CleanProbe}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["Clean"]

    @pytest.mark.asyncio
    async def test_violating_class_is_skipped(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"Violator": _ViolatingProbe, "Clean": _CleanProbe}):
            schemas = await manager._serialize_library_node_schemas("libA")

        # Violating class dropped from output even though it only warns on the
        # orchestrator: the drop is gated on drops_class_from_schema, not severity.
        assert [s.class_name for s in schemas] == ["Clean"]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("fixture probe violation" in r.getMessage() for r in warnings)
        assert any("class=Violator" in r.getMessage() for r in warnings)


class _ProbeWithHuggingFaceRepoParam(MockNode):
    """Node whose __init__ builds the engine's HF repo dropdown — the DA3/diffusers pattern.

    Regression for the reentrant-bus-in-init violations the component itself used to commit:
    its policy and download queries fired from inside node __init__, so every library using it
    (Depth Anything 3, diffusers, …) had its classes dropped from the worker schema.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        repo_param = HuggingFaceRepoParameter(self, repo_ids=["owner/model-a"])
        repo_param.add_input_parameters()


class TestHuggingFaceRepoParameterSurvivesTheProbe:
    @pytest.mark.asyncio
    async def test_hf_param_node_is_included_and_issues_no_bus_requests(
        self, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        module = "griptape_nodes.exe_types.param_components.huggingface"

        def _refuse_bus(request: object) -> object:
            msg = f"bus request issued during probe construction: {type(request).__name__}"
            raise AssertionError(msg)

        manager = current_engine().library_manager
        with (
            patch(f"{module}.huggingface_repo_parameter.list_repo_revisions_in_cache", return_value=[]),
            patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=_refuse_bus),
            patched_registry({"HFNode": _ProbeWithHuggingFaceRepoParam}),
        ):
            schemas = await manager._serialize_library_node_schemas("libA")

        # Before the construction-time deferral, the component's bus requests fired
        # reentrant-bus-in-init here and the class was dropped from the schemas.
        assert [s.class_name for s in schemas] == ["HFNode"]


class _DummyTrait(Trait):
    """Minimal concrete Trait used to exercise the trait-detection path."""

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["dummy"]


class _ProbeWithConverterParam:
    """Node class whose probe exposes a Parameter with a user-attached converter."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parameters = [
            Parameter(name="p_with_converter", converters=[lambda v: v]),
        ]


class _ProbeWithValidatorParam:
    """Node class whose probe exposes a Parameter with a user-attached validator."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parameters = [
            Parameter(name="p_with_validator", validators=[lambda _p, _v: None]),
        ]


class _ProbeWithTraitParam:
    """Node class whose probe exposes a Parameter with a real Trait child."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parameters = [
            Parameter(name="p_with_trait", traits={_DummyTrait()}),
        ]


class _ProbeWithCleanParams:
    """Node class whose probe parameter has no converters/validators/traits."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parameters = [Parameter(name="p_clean")]


class TestParameterBehaviorsDropped:
    """#4472: Parameters carrying converters/validators/traits emit a warn violation."""

    @pytest.mark.asyncio
    async def test_clean_parameters_produce_no_violation(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"Clean": _ProbeWithCleanParams}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["Clean"]
        assert not any("p_clean" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_parameter_with_converter_reports_warning_but_keeps_schema(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"WithBehavior": _ProbeWithConverterParam}):
            schemas = await manager._serialize_library_node_schemas("libA")

        # Warning, not error: the class still yields a schema.
        assert [s.class_name for s in schemas] == ["WithBehavior"]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("p_with_converter" in r.getMessage() for r in warnings)
        assert any("converters" in r.getMessage() for r in warnings)
        assert any("class=WithBehavior" in r.getMessage() for r in warnings)

    @pytest.mark.asyncio
    async def test_parameter_with_validator_reports_warning_but_keeps_schema(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"WithValidator": _ProbeWithValidatorParam}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["WithValidator"]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("p_with_validator" in r.getMessage() for r in warnings)
        assert any("validators" in r.getMessage() for r in warnings)

    @pytest.mark.asyncio
    async def test_parameter_with_trait_reports_warning_but_keeps_schema(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"WithTrait": _ProbeWithTraitParam}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["WithTrait"]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("p_with_trait" in r.getMessage() for r in warnings)
        assert any("traits" in r.getMessage() for r in warnings)


class _ProbeWithConnectionHook:
    """Node class overriding a connection lifecycle hook in library code."""

    parameters: list = []  # noqa: RUF012

    def __init__(self, name: str) -> None:
        self.name = name

    def after_incoming_connection(self, source_node: Any, source_parameter: Any, target_parameter: Any) -> None:
        pass


class _ProbeWithValueHook:
    """Node class overriding a value hook in library code."""

    parameters: list = []  # noqa: RUF012

    def __init__(self, name: str) -> None:
        self.name = name

    def after_value_set(self, parameter: Any, value: Any) -> None:
        pass


class _EngineOwnedHookMixin:
    """Stands in for an engine-owned component whose hook override is not the author's code."""

    def after_value_set(self, parameter: Any, value: Any) -> None:
        pass


# The detector attributes an override to its defining class's module; stamp an
# engine-namespace module so this mixin reads as engine-owned.
_EngineOwnedHookMixin.__module__ = "griptape_nodes.exe_types.param_components.fake_component"


class _ProbeInheritingEngineHook(_EngineOwnedHookMixin):
    """Node class whose only hook override comes from an engine-owned base."""

    parameters: list = []  # noqa: RUF012

    def __init__(self, name: str) -> None:
        self.name = name


class TestInertWorkerHooks:
    """#5314: hook overrides that never fire under isolation emit a warn violation."""

    @pytest.mark.asyncio
    async def test_connection_hook_override_reports_warning_but_keeps_schema(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"WithConnHook": _ProbeWithConnectionHook}):
            schemas = await manager._serialize_library_node_schemas("libA")

        # Warning, not error: the class still yields a schema.
        assert [s.class_name for s in schemas] == ["WithConnHook"]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("after_incoming_connection" in r.getMessage() for r in warnings)
        assert any("class=WithConnHook" in r.getMessage() for r in warnings)
        assert any("Shared mode" in r.getMessage() for r in warnings)

    @pytest.mark.asyncio
    async def test_value_hook_override_reports_warning_but_keeps_schema(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"WithValueHook": _ProbeWithValueHook}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["WithValueHook"]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("after_value_set" in r.getMessage() for r in warnings)
        assert any("class=WithValueHook" in r.getMessage() for r in warnings)
        assert any("input hydration" in r.getMessage() for r in warnings)

    @pytest.mark.asyncio
    async def test_engine_owned_hook_override_is_not_reported(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        # A hook implemented by an engine-owned base/component (module under
        # griptape_nodes.) is not the author's code; flagging it would nag on
        # something the library author cannot remediate.
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"EngineHook": _ProbeInheritingEngineHook}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["EngineHook"]
        assert not any("after_value_set" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_hookless_class_produces_no_hook_violation(
        self, caplog: pytest.LogCaptureFixture, patched_registry: Callable[[dict[str, type]], Any]
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes.strict_mode")
        manager = current_engine().library_manager
        with patched_registry({"Clean": _CleanProbe}):
            schemas = await manager._serialize_library_node_schemas("libA")

        assert [s.class_name for s in schemas] == ["Clean"]
        assert not any("hook" in r.getMessage().lower() for r in caplog.records)
