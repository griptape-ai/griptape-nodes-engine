"""End-to-end tests for the exec-dep worker MVP.

A library that declares execution dependencies (``pip_dependencies_exec``) gets a different
deal than either mode that exists today:

- It loads REAL node classes on the orchestrator (not schema stubs), because its node
  modules import with edit-time deps only.
- Its nodes EXECUTE in its dedicated worker process, where ``.venv-exec`` is on
  ``sys.path``.
- While executing there, node code cannot reach orchestrator-owned managers directly; it
  asks the orchestrator with a request instead.
- A value it produces that cannot leave that process fails loudly with instructions.

These tests pin those four behaviors, plus the crucial negative: a library that declares no
execution dependencies is untouched by any of it.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.app_events import LibraryLoadedNotification
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    SendNodeMessageRequest,
    SendNodeMessageResultSuccess,
)

# The facade import survives for the dummy-manager guard tests, whose subject it is.
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes  # noqa: TID251
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload
from griptape_nodes.utils.version_utils import engine_version
from tests.e2e.fixtures.behavior_preservation_library.behavior_preservation_node import CLICK_ACKNOWLEDGEMENT
from tests.e2e.offline_wheels import build_wheel, offline_install_flags

if TYPE_CHECKING:
    from collections.abc import Iterator

EXEC_FIXTURE = Path(__file__).parent / "fixtures" / "exec_dep_library"
NONSERIALIZABLE_FIXTURE = Path(__file__).parent / "fixtures" / "nonserializable_library"
BEHAVIOR_FIXTURE = Path(__file__).parent / "fixtures" / "behavior_preservation_library"


def _register(  # noqa: PLR0913 (a test-library builder; each knob is one manifest field)
    tmp_path: Path,
    *,
    fixture_dir: Path,
    node_file: str,
    name: str,
    exec_dependencies: list[str] | None = None,
    edit_dependencies: list[str] | None = None,
    required_resources: dict[str, object] | None = None,
) -> str:
    """Register a fixture library, optionally declaring execution dependencies."""
    library_dir = tmp_path / name.replace(" ", "_")
    library_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads((fixture_dir / "griptape_nodes_library.json").read_text())
    schema["name"] = name
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    dependencies: dict[str, list[str]] = {"pip_dependencies": list(edit_dependencies or [])}
    if exec_dependencies:
        wheel_dir = tmp_path / "wheels"
        for dep in [*(edit_dependencies or []), *exec_dependencies]:
            build_wheel(wheel_dir, dep, "1.0.0")
        dependencies["pip_dependencies_exec"] = exec_dependencies
        dependencies["pip_install_flags"] = offline_install_flags(wheel_dir)
    elif edit_dependencies:
        wheel_dir = tmp_path / "wheels"
        for dep in edit_dependencies:
            build_wheel(wheel_dir, dep, "1.0.0")
        dependencies["pip_install_flags"] = offline_install_flags(wheel_dir)
    schema["metadata"]["dependencies"] = dependencies
    if required_resources is not None:
        resources: dict[str, object] = {}
        if required_resources is not None:
            resources["required"] = required_resources
        schema["metadata"]["resources"] = resources
    library_json = library_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    shutil.copy(fixture_dir / node_file, library_dir / node_file)
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return str(library_json)


def _library_info(library_json: str) -> LibraryManager.LibraryInfo:
    return current_engine().library_manager._library_file_path_to_info[library_json]


EDIT_DEP = "fakeedit"
EXEC_DEP = "fakeexec"


@pytest.fixture(autouse=True)
def _isolate_import_state() -> Iterator[None]:
    """Keep sys.path and sys.modules from leaking between tests.

    Every registration inserts paths and imports modules. Without this, a dependency imported
    by one test would satisfy the next test's import from the module cache, which would hide
    exactly the wiring these tests exist to check.
    """
    original_path = list(sys.path)
    original_is_worker = current_engine().library_manager._is_worker
    for dep in (EDIT_DEP, EXEC_DEP):
        sys.modules.pop(dep, None)
    yield
    sys.path[:] = original_path
    current_engine().library_manager._is_worker = original_is_worker
    for dep in (EDIT_DEP, EXEC_DEP):
        sys.modules.pop(dep, None)


class TestRoutingFact:
    def test_exec_dependencies_mean_execution_in_a_worker(self, tmp_path: Path) -> None:
        """Declaring exec deps routes execution to a worker WITHOUT the legacy stub path."""
        library_json = _register(
            tmp_path,
            fixture_dir=EXEC_FIXTURE,
            node_file="exec_dep_node.py",
            name="Routing Heavy",
            edit_dependencies=["fakeedit"],
            exec_dependencies=["fakeexec"],
        )

        info = _library_info(library_json)
        assert info.executes_in_worker is True
        # Crucially NOT the legacy worker mode: no stub path, no WORKER_PENDING gating.
        assert info.requires_worker is False
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED

    def test_no_exec_dependencies_means_no_worker(self, tmp_path: Path) -> None:
        library_json = _register(
            tmp_path,
            fixture_dir=NONSERIALIZABLE_FIXTURE,
            node_file="nonserializable_nodes.py",
            name="Routing Light",
        )

        info = _library_info(library_json)
        assert info.executes_in_worker is False
        assert info.requires_worker is False

    def test_worker_is_required_once_declared(self, tmp_path: Path) -> None:
        """With no worker registered, routing raises instead of silently running locally.

        Falling back to in-process execution would run the node in the one process that
        deliberately lacks its execution dependencies.
        """
        _register(
            tmp_path,
            fixture_dir=EXEC_FIXTURE,
            node_file="exec_dep_node.py",
            name="Routing Guard",
            edit_dependencies=["fakeedit"],
            exec_dependencies=["fakeexec"],
        )

        with pytest.raises(RuntimeError, match="requires a dedicated worker"):
            current_engine().library_manager.get_worker_for_library("Routing Guard")


class TestRealNodesOnOrchestrator:
    def test_orchestrator_holds_real_node_classes_not_stubs(self, tmp_path: Path) -> None:
        """The point of the dep split: real classes in the editor, heavy deps elsewhere.

        The node instantiates here with its edit-time dependency importable and its
        execution dependency absent from this process.
        """
        _register(
            tmp_path,
            fixture_dir=EXEC_FIXTURE,
            node_file="exec_dep_node.py",
            name="Real Nodes",
            edit_dependencies=["fakeedit"],
            exec_dependencies=["fakeexec"],
        )

        node = LibraryRegistry.create_node(node_type="ExecDepNode", name="real", specific_library_name="Real Nodes")

        # A stub would carry parameter shape only; this is the real class, so its
        # __init__ ran and read its edit-time dependency.
        assert node.get_parameter_value("edit_dep_version") == "1.0.0"
        assert type(node).__name__ == "ExecDepNode"
        assert type(node).__module__ != "griptape_nodes.retained_mode.managers.library_manager"


class TestParameterBehaviorsSurviveOnRealClasses:
    """What a schema stub drops, and what these libraries keep by not being stubbed.

    A stub is rebuilt from ``WorkerParameterSchema``, which carries scalar fields and
    ``ui_options`` only. Traits, converters and validators live in Python and cannot travel,
    so a worker-mode library loses all of them on the orchestrator -- see
    griptape-nodes-engine#5420 for the trait case, which is the worst of the family because
    ``ui_options`` DO serialize: the button renders, looks clickable, and cannot work.

    An execution-dependency library sets ``executes_in_worker`` WITHOUT setting
    ``requires_worker``, and stub registration is gated on the latter, so these libraries keep
    the real class. ``test_the_orchestrator_refuses_worker_schemas_that_would_clobber_real_classes``
    is the one that drives that gate; the rest cover what survives on the class itself.
    """

    def _register_behavior_library(self, tmp_path: Path) -> str:
        return _register(
            tmp_path,
            fixture_dir=BEHAVIOR_FIXTURE,
            node_file="behavior_preservation_node.py",
            name="Behavior Library",
            exec_dependencies=["fakeexec"],
        )

    def test_a_button_trait_survives_and_its_click_resolves(self, tmp_path: Path) -> None:
        """The #5420 case: the trait is present, so the click reaches the node's handler."""
        self._register_behavior_library(tmp_path)
        node = LibraryRegistry.create_node(
            node_type="BehaviorPreservationNode", name="behaviors", specific_library_name="Behavior Library"
        )
        current_engine().object_manager.add_object_by_name("behaviors", node)

        parameter = node.get_parameter_by_name("model_manager")
        assert parameter is not None
        assert list(parameter.find_elements_by_type(Button)), "the Button trait did not survive"

        result = current_engine().handle_request(
            SendNodeMessageRequest(
                node_name="behaviors",
                optional_element_name="model_manager",
                message_type="on_click",
                message=ButtonDetailsMessagePayload(label="Open", variant="default", size="md", state="ready"),
            )
        )

        assert isinstance(result, SendNodeMessageResultSuccess), getattr(result, "result_details", result)
        # The trait's bound callback is what produced this, so the string proves the node's own
        # handler ran rather than something merely accepting the message.
        assert CLICK_ACKNOWLEDGEMENT in str(result.result_details)

    @pytest.mark.asyncio
    async def test_the_orchestrator_refuses_worker_schemas_that_would_clobber_real_classes(
        self, tmp_path: Path
    ) -> None:
        """Drive the gate itself: a worker's schemas must not replace real classes.

        The tests above load a library in-process, which is true of any library and never
        reaches the gate. The clobber happens later and elsewhere: the worker sends a
        LibraryLoadedNotification carrying serialized schemas, and the ORCHESTRATOR decides
        whether to build stub classes from them. `Library.register_new_node_type` overwrites
        unconditionally, so a gate keyed on the wrong flag silently swaps this library's real
        classes -- traits and all -- for stubs after load.
        """
        self._register_behavior_library(tmp_path)
        library_manager = current_engine().library_manager
        library_info = library_manager.get_library_info_by_library_name("Behavior Library")
        assert library_info is not None
        assert library_info.executes_in_worker is True, "the library must be worker-routed for this to mean anything"
        assert library_info.requires_worker is False, "...but not via the legacy stub path"

        # Exactly what a worker broadcasts after loading the library.
        schemas = await library_manager._serialize_library_node_schemas("Behavior Library")
        await library_manager._on_library_loaded_notification(
            LibraryLoadedNotification(
                library_name="Behavior Library",
                fitness=LibraryManager.LibraryFitness.GOOD,
                node_schemas=schemas,
            )
        )

        node = LibraryRegistry.create_node(
            node_type="BehaviorPreservationNode", name="post_notification", specific_library_name="Behavior Library"
        )
        parameter = node.get_parameter_by_name("model_manager")
        assert parameter is not None
        assert list(parameter.find_elements_by_type(Button)), (
            "the worker's schemas replaced the real class with a stub, dropping the Button trait"
        )

    def test_converters_and_validators_survive(self, tmp_path: Path) -> None:
        """The quieter half of the same family: both only exist as Python callables."""
        self._register_behavior_library(tmp_path)
        node = LibraryRegistry.create_node(
            node_type="BehaviorPreservationNode", name="callables", specific_library_name="Behavior Library"
        )

        node.set_parameter_value("mode", "expand")
        assert node.get_parameter_value("mode") == "EXPAND", "the converter did not run"

        with pytest.raises(ValueError, match="forbidden"):
            node.set_parameter_value("mode", "FORBIDDEN")

    @pytest.mark.asyncio
    async def test_the_stub_loss_warnings_stay_quiet_for_a_library_that_is_not_stubbed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The detectors describe what a stub loses, so they must not fire when there is none.

        Both fire from the worker-side schema probe, which runs for every library a worker
        loads -- including execution-dependency libraries, whose schemas the orchestrator
        then ignores. Ungated, this node's converter, validator and trait each produced a
        warning saying they "will not execute on the orchestrator stub", for a library whose
        orchestrator copy is the real class. That sends an author to fix correct code.
        """
        self._register_behavior_library(tmp_path)
        library_manager = current_engine().library_manager

        with caplog.at_level(logging.WARNING):
            await library_manager._serialize_library_node_schemas("Behavior Library")

        assert "will not execute on the orchestrator stub" not in caplog.text
        assert "parameter-behaviors-dropped-in-schema" not in caplog.text
        # The other two gated rules carry their own wording; assert each, or half the gate
        # can be reverted with nothing failing.
        assert "connection hooks run on the orchestrator against a stub" not in caplog.text
        assert "these fire only during" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_stub_loss_warnings_still_fire_for_a_legacy_worker_mode_library(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other direction: a library that IS stubbed still gets told what it loses."""
        self._register_behavior_library(tmp_path)
        library_manager = current_engine().library_manager
        library_info = library_manager.get_library_info_by_library_name("Behavior Library")
        assert library_info is not None
        library_info.requires_worker = True

        with caplog.at_level(logging.WARNING):
            await library_manager._serialize_library_node_schemas("Behavior Library")

        assert "will not execute on the orchestrator stub" in caplog.text
        assert "connection hooks run on the orchestrator against a stub" in caplog.text
        assert "these fire only during" in caplog.text

    @pytest.mark.asyncio
    async def test_the_stub_path_these_libraries_avoid_would_drop_the_button(self, tmp_path: Path) -> None:
        """Negative control: the same parameters through stub synthesis lose the trait.

        Without this, the test above passes for any ordinary library and proves nothing about
        the routing decision. Driving the real serializer and stub builder shows the loss is in
        stub synthesis, and reproduces the reported failure message exactly.
        """
        self._register_behavior_library(tmp_path)
        library_manager = current_engine().library_manager

        schemas = await library_manager._serialize_library_node_schemas("Behavior Library")
        node_schema = next(s for s in schemas if s.class_name == "BehaviorPreservationNode")
        stub_class = library_manager._make_worker_stub_class("BehaviorPreservationNode", node_schema.parameters)
        stub = stub_class(name="stubbed")
        current_engine().object_manager.add_object_by_name("stubbed", stub)

        stub_parameter = stub.get_parameter_by_name("model_manager")
        assert stub_parameter is not None, "the parameter itself should survive -- only its trait is lost"
        assert not list(stub_parameter.find_elements_by_type(Button))

        result = current_engine().handle_request(
            SendNodeMessageRequest(
                node_name="stubbed",
                optional_element_name="model_manager",
                message_type="on_click",
                message=ButtonDetailsMessagePayload(label="Open", variant="default", size="md", state="ready"),
            )
        )

        assert not isinstance(result, SendNodeMessageResultSuccess)
        assert "no handler was available" in str(result.result_details)


class TestUnmetResourcesCostExecutionNotLoading:
    """A capability this machine lacks must not take the library away.

    Nothing about a missing GPU stops the orchestrator importing base-clean node modules,
    drawing their parameters, or saving a workflow that uses them. Refusing to REGISTER took
    all of that away: fitness UNUSABLE, no node types, and placeholder nodes reading "Library
    not found" on every machine that was only ever going to edit. This is the same rule the
    engine applies to a dependency that will not install -- the capability gates the run.
    """

    IMPOSSIBLE_COMPUTE: ClassVar[dict[str, object]] = {"compute": [["definitely-not-a-real-backend"], "has_any"]}

    def test_the_library_still_registers_and_its_nodes_still_load(self, tmp_path: Path) -> None:
        library_json = _register(
            tmp_path,
            fixture_dir=BEHAVIOR_FIXTURE,
            node_file="behavior_preservation_node.py",
            name="Unmet Resources",
            required_resources=self.IMPOSSIBLE_COMPUTE,
        )

        info = _library_info(library_json)
        assert info.lifecycle_state is not LibraryManager.LibraryLifecycleState.FAILURE
        assert info.fitness is LibraryManager.LibraryFitness.FLAWED
        node = LibraryRegistry.create_node(
            node_type="BehaviorPreservationNode", name="editable", specific_library_name="Unmet Resources"
        )
        assert node.get_parameter_by_name("mode") is not None

    def test_execution_refuses_and_names_the_capability(self, tmp_path: Path) -> None:
        _register(
            tmp_path,
            fixture_dir=BEHAVIOR_FIXTURE,
            node_file="behavior_preservation_node.py",
            name="Unmet Resources Refuses",
            required_resources=self.IMPOSSIBLE_COMPUTE,
        )

        with pytest.raises(RuntimeError) as excinfo:
            current_engine().library_manager.get_worker_for_library("Unmet Resources Refuses")

        message = str(excinfo.value)
        assert "definitely-not-a-real-backend" in message, "the artist is not told what is missing"
        assert "Editing its nodes still works" in message

    def test_a_library_whose_resources_are_met_is_unaffected(self, tmp_path: Path) -> None:
        """The gate must not fire for a requirement this machine does satisfy."""
        library_json = _register(
            tmp_path,
            fixture_dir=BEHAVIOR_FIXTURE,
            node_file="behavior_preservation_node.py",
            name="Met Resources",
            required_resources={"compute": [["cpu"], "has_any"]},
        )

        info = _library_info(library_json)
        assert info.execution_unavailable_reason is None
        assert info.fitness is LibraryManager.LibraryFitness.GOOD


class TestUnmetRequirementCostsExecutionNotEditing:
    """A resource this machine lacks must not stop the library loading.

    SAM3 declares cuda-only, and refusing to register it took editing away from every machine that
    was only ever going to edit -- placeholder nodes reading "Library not found" on a laptop. The
    requirement now gates the run instead, so the refusal arrives when someone presses run.
    """

    IMPOSSIBLE_COMPUTE: ClassVar[dict[str, object]] = {"compute": [["definitely-not-a-real-backend"], "has_any"]}

    def test_execution_refuses_for_an_unmet_requirement(self, tmp_path: Path) -> None:
        _register(
            tmp_path,
            fixture_dir=BEHAVIOR_FIXTURE,
            node_file="behavior_preservation_node.py",
            name="Requires The Impossible",
            required_resources=self.IMPOSSIBLE_COMPUTE,
        )

        with pytest.raises(RuntimeError, match="definitely-not-a-real-backend"):
            current_engine().library_manager.get_worker_for_library("Requires The Impossible")


class TestUnshippableOutputGuardrail:
    @pytest.mark.asyncio
    async def test_worker_producing_an_unshippable_value_fails_with_instructions(self, tmp_path: Path) -> None:
        """A serializable=False output cannot cross the boundary, so say so usefully."""
        from griptape_nodes.retained_mode.events.execution_events import (
            ExecuteNodeRequest,
            ExecuteNodeResultFailure,
        )

        _register(
            tmp_path,
            fixture_dir=NONSERIALIZABLE_FIXTURE,
            node_file="nonserializable_nodes.py",
            name="Guardrail Library",
        )
        library_manager = current_engine().library_manager
        library_manager._is_worker = True
        node = LibraryRegistry.create_node(
            node_type="ProducerNode", name="Producer", specific_library_name="Guardrail Library"
        )
        current_engine().object_manager.add_object_by_name("Producer", node)

        result = await current_engine().ahandle_request(
            ExecuteNodeRequest(
                node_name="Producer",
                node_metadata={"node_type": "ProducerNode", "library": "Guardrail Library"},
            )
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        details = str(result.result_details)
        assert "'session'" in details
        assert "cannot leave" in details
        # The message must teach the way forward, not just refuse.
        assert "descriptor" in details

    @pytest.mark.asyncio
    async def test_serializable_outputs_are_unaffected(self, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.events.execution_events import (
            ExecuteNodeRequest,
            ExecuteNodeResultSuccess,
        )

        # Set BEFORE registering: the execution venv is spliced onto sys.path during load, and only
        # for a worker, so flipping the flag afterwards leaves the heavy import unavailable.
        current_engine().library_manager._is_worker = True
        _register(
            tmp_path,
            fixture_dir=EXEC_FIXTURE,
            node_file="exec_dep_node.py",
            name="Guardrail Serializable",
            edit_dependencies=["fakeedit"],
            # Installed so process() actually completes. Without it the node raised on the missing
            # import every time, so the branch this test is named for never ran and the assertion
            # below could not distinguish success from any unrelated failure.
            exec_dependencies=["fakeexec"],
        )
        node = LibraryRegistry.create_node(
            node_type="ExecDepNode", name="Fine", specific_library_name="Guardrail Serializable"
        )
        current_engine().object_manager.add_object_by_name("Fine", node)

        result = await current_engine().ahandle_request(
            ExecuteNodeRequest(
                node_name="Fine",
                node_metadata={"node_type": "ExecDepNode", "library": "Guardrail Serializable"},
            )
        )

        # A node whose outputs are all serializable must ship them, so the unshippable-output
        # guardrail must NOT fire. Asserted on success rather than either-branch: the previous
        # version passed whether or not the node ran.
        assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
        # This suite builds both wheels at 1.0.0; the version itself is not the point, having
        # BOTH outputs is -- one proves the edit environment, the other the execution one.
        assert result.parameter_output_values["edit_dep_version"] == "1.0.0"
        assert result.parameter_output_values["exec_dep_version"] == "1.0.0"


class TestManagerAccessDuringWorkerExecution:
    def test_config_manager_is_refused_inside_worker_execution(self) -> None:
        """In a worker, node code must ask the orchestrator rather than read local state."""
        library_manager = current_engine().library_manager
        library_manager._is_worker = True
        event_manager = current_engine().event_manager

        with event_manager.worker_node_execution_scope(), pytest.raises(RuntimeError) as excinfo:
            GriptapeNodes.ConfigManager()

        message = str(excinfo.value)
        assert "ConfigManager" in message
        assert "GetConfigValueRequest" in message

    def test_secrets_and_os_managers_are_refused_too(self) -> None:
        current_engine().library_manager._is_worker = True
        event_manager = current_engine().event_manager

        with event_manager.worker_node_execution_scope():
            with pytest.raises(RuntimeError, match="GetSecretValueRequest"):
                GriptapeNodes.SecretsManager()
            with pytest.raises(RuntimeError, match="ReadFileRequest"):
                GriptapeNodes.OSManager()

    def test_the_guard_covers_every_manager_but_static_files(self) -> None:
        """The guard is broad by design: silently-wrong local answers are worse than errors.

        Sweeps every manager accessor on the facade rather than naming a few, so adding an
        accessor without deciding its worker story fails here instead of shipping unguarded.
        """
        current_engine().library_manager._is_worker = True
        event_manager = current_engine().event_manager

        minimum_believable_sweep = 20
        accessors = [
            name
            for name, member in vars(GriptapeNodes).items()
            if isinstance(member, classmethod) and name[0].isupper() and name.endswith("Manager")
        ]
        assert len(accessors) > minimum_believable_sweep, "sweep found too few accessors to be believed"

        with event_manager.worker_node_execution_scope():
            for name in accessors:
                if name == "StaticFilesManager":
                    assert getattr(GriptapeNodes, name)() is not None
                    continue
                with pytest.raises(RuntimeError, match=name):
                    getattr(GriptapeNodes, name)()

    def test_managers_work_outside_node_execution_in_a_worker(self) -> None:
        """Engine boot and library load happen in the worker too, and need real managers."""
        current_engine().library_manager._is_worker = True

        assert GriptapeNodes.ConfigManager() is not None
        assert GriptapeNodes.SecretsManager() is not None

    def test_managers_work_during_execution_on_the_orchestrator(self) -> None:
        """The refusal is about being in a worker, not about executing."""
        event_manager = current_engine().event_manager

        with event_manager.worker_node_execution_scope():
            assert GriptapeNodes.ConfigManager() is not None

    def test_static_files_manager_stays_available(self) -> None:
        """Storage is the one local capability: media bytes cannot cross the boundary."""
        current_engine().library_manager._is_worker = True
        event_manager = current_engine().event_manager

        with event_manager.worker_node_execution_scope():
            assert GriptapeNodes.StaticFilesManager() is not None
