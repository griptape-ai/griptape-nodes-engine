"""Unit tests for LocalWorkflowExecutor project activation, CLI arguments, and flow input."""

import os
from argparse import ArgumentParser
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
    STORAGE_BACKEND_ENV_VAR,
    LocalExecutorError,
    LocalWorkflowExecutor,
)
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.exe_types.node_types import StartNode
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.project_events import (
    ActivateWorkspaceProjectRequest,
    ActivateWorkspaceProjectResultFailure,
    ActivateWorkspaceProjectResultSuccess,
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectResultSuccess,
)
from griptape_nodes.serialization.values import encode_value

# A Windows path that exceeds the legacy MAX_PATH (260 chars).
# Total length is ~300 characters including the drive letter.
_LONG_WINDOWS_PATH = PureWindowsPath(
    "C:\\Users\\SomeUser\\AppData\\Local\\GriptapeNodes\\Projects\\"
    + "\\".join(["a_rather_long_directory_name_that_pads_length"] * 5)
    + "\\my_project_template.yaml"
)

WINDOWS_MAX_PATH = 260
EXPECTED_REQUEST_COUNT = 2
MODULE_PATH = "griptape_nodes.bootstrap.workflow_executors.local_workflow_executor"


class TestLoadProject:
    """Tests for LocalWorkflowExecutor._load_project."""

    @pytest.mark.asyncio
    async def test_load_project_with_long_windows_path(self) -> None:
        """A Windows path exceeding MAX_PATH (260 chars) should be accepted."""
        assert len(str(_LONG_WINDOWS_PATH)) > WINDOWS_MAX_PATH

        mock_load_result = MagicMock(spec=LoadProjectTemplateResultSuccess)
        mock_load_result.project_id = "test-project-id"

        mock_set_result = MagicMock(spec=SetCurrentProjectResultSuccess)
        mock_set_result.failed.return_value = False

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(side_effect=[mock_load_result, mock_set_result])

            executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
            project_path = Path(_LONG_WINDOWS_PATH)
            await executor._load_project(project_path)

        calls = mock_gn.ahandle_request.call_args_list
        assert len(calls) == EXPECTED_REQUEST_COUNT
        # Verify the long path was passed through to LoadProjectTemplateRequest
        load_request = calls[0].args[0]
        assert load_request.project_path == project_path

    @pytest.mark.asyncio
    async def test_load_project_raises_on_load_failure(self) -> None:
        """_load_project should raise LocalExecutorError when loading fails."""
        # Return something that is NOT a LoadProjectTemplateResultSuccess
        mock_load_result = MagicMock(spec=[])

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(return_value=mock_load_result)

            executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
            with pytest.raises(LocalExecutorError, match="Attempted to load project template from"):
                await executor._load_project(Path("/some/project.yaml"))

    @pytest.mark.asyncio
    async def test_load_project_raises_on_set_current_failure(self) -> None:
        """_load_project should raise LocalExecutorError when setting current project fails."""
        mock_load_result = MagicMock(spec=LoadProjectTemplateResultSuccess)
        mock_load_result.project_id = "test-project-id"

        mock_set_result = MagicMock()
        mock_set_result.failed.return_value = True

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(side_effect=[mock_load_result, mock_set_result])

            executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
            with pytest.raises(LocalExecutorError, match="Attempted to set project"):
                await executor._load_project(Path("/some/project.yaml"))

    @pytest.mark.asyncio
    async def test_load_project_success(self) -> None:
        """_load_project should complete without error on success."""
        mock_load_result = MagicMock(spec=LoadProjectTemplateResultSuccess)
        mock_load_result.project_id = "proj-123"

        mock_set_result = MagicMock(spec=SetCurrentProjectResultSuccess)
        mock_set_result.failed.return_value = False

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(side_effect=[mock_load_result, mock_set_result])

            executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
            await executor._load_project(Path("/some/project.yaml"))

        # Verify both requests were made
        assert mock_gn.ahandle_request.call_count == EXPECTED_REQUEST_COUNT

    @pytest.mark.asyncio
    async def test_load_project_activates_by_opaque_id_not_path(self) -> None:
        """The executor must activate by the load result's opaque id, never the file path (internal#110)."""
        # The opaque id the engine echoes back differs from the on-disk path, so a
        # path-based activation would not match it.
        opaque_id = "deadbeef-0000-1111-2222-333344445555"
        mock_load_result = MagicMock(spec=LoadProjectTemplateResultSuccess)
        mock_load_result.project_id = opaque_id

        mock_set_result = MagicMock(spec=SetCurrentProjectResultSuccess)
        mock_set_result.failed.return_value = False

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(side_effect=[mock_load_result, mock_set_result])

            executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
            await executor._load_project(Path("/some/project.yaml"))

        set_request = mock_gn.ahandle_request.call_args_list[1].args[0]
        assert set_request.project_id == opaque_id


class TestEnterActivatesProject:
    """`__aenter__` activates a project before broadcasting AppInitializationComplete (#6214)."""

    @staticmethod
    def _executor(project_file_path: Path | None = None) -> LocalWorkflowExecutor:
        executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
        executor._project_file_path = project_file_path
        executor._skip_library_loading = True
        executor._workflows_to_register = []
        return executor

    @staticmethod
    def _broadcasts(mock_gn: MagicMock) -> list[object]:
        return [call.args[0] for call in mock_gn.EventManager.return_value.abroadcast_app_event.call_args_list]

    @pytest.mark.asyncio
    async def test_activates_configured_project_before_app_initialization(self) -> None:
        """Without --project-file-path, the configured project is activated before boot completes."""
        order: list[str] = []
        activated = ActivateWorkspaceProjectResultSuccess(result_details="Activated workspace project: p")

        async def handle_request(request: object) -> object:
            order.append(type(request).__name__)
            return activated

        async def broadcast(event: object) -> None:
            order.append(type(event).__name__)

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(side_effect=handle_request)
            mock_gn.EventManager.return_value.abroadcast_app_event = AsyncMock(side_effect=broadcast)

            await self._executor().__aenter__()

        assert order == [ActivateWorkspaceProjectRequest.__name__, AppInitializationComplete.__name__]

    @pytest.mark.asyncio
    async def test_activation_fails_if_project_file_missing(self) -> None:
        """The executor asks for a missing project_file to fail rather than fall back to the workspace project."""
        activated = ActivateWorkspaceProjectResultSuccess(result_details="Activated workspace project: p")

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(return_value=activated)
            mock_gn.EventManager.return_value.abroadcast_app_event = AsyncMock()

            await self._executor().__aenter__()

        request = mock_gn.ahandle_request.call_args.args[0]
        assert isinstance(request, ActivateWorkspaceProjectRequest)
        assert request.fail_if_project_file_missing is True

    @pytest.mark.asyncio
    async def test_configured_project_that_fails_to_activate_fails_the_run(self) -> None:
        """A configured project that is denied or broken raises instead of falling back to system defaults."""
        denied = ActivateWorkspaceProjectResultFailure(
            result_details="Attempted to activate workspace project at '/p/griptape-nodes-project.yml'. "
            "Failed because Denied by the license policy."
        )

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(return_value=denied)
            mock_gn.EventManager.return_value.abroadcast_app_event = AsyncMock()

            with pytest.raises(LocalExecutorError, match="Denied by the license policy"):
                await self._executor().__aenter__()

            assert self._broadcasts(mock_gn) == []

    @pytest.mark.asyncio
    async def test_no_configured_project_stays_on_system_defaults(self) -> None:
        """With nothing configured the activation is a no-op success and the run proceeds."""
        no_project = ActivateWorkspaceProjectResultSuccess(
            result_details="No workspace project found; system defaults remain active"
        )

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(return_value=no_project)
            mock_gn.EventManager.return_value.abroadcast_app_event = AsyncMock()

            executor = self._executor()
            assert await executor.__aenter__() is executor

            broadcasts = self._broadcasts(mock_gn)
            assert len(broadcasts) == 1
            assert isinstance(broadcasts[0], AppInitializationComplete)

    @pytest.mark.asyncio
    async def test_explicit_project_file_path_skips_workspace_activation(self) -> None:
        """--project-file-path is loaded directly; the configured project is not consulted."""
        mock_load_result = MagicMock(spec=LoadProjectTemplateResultSuccess)
        mock_load_result.project_id = "proj-123"
        mock_set_result = MagicMock(spec=SetCurrentProjectResultSuccess)
        mock_set_result.failed.return_value = False

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(side_effect=[mock_load_result, mock_set_result])
            mock_gn.EventManager.return_value.abroadcast_app_event = AsyncMock()

            await self._executor(Path("/some/project.yaml")).__aenter__()

        requests = [call.args[0] for call in mock_gn.ahandle_request.call_args_list]
        assert isinstance(requests[0], LoadProjectTemplateRequest)
        assert not any(isinstance(request, ActivateWorkspaceProjectRequest) for request in requests)


class TestPrepareWorkflowForRunStorageBackend:
    """Regression tests for issue #4828.

    Generated workflow files splat the same `**kwargs` into both the
    `LocalWorkflowExecutor(...)` constructor and `executor.arun(...)`, so a
    `storage_backend` forwarded through `execute_workflow(**kwargs)` reaches the
    run path. The run path used to hard-raise on any non-None `storage_backend`,
    turning a valid caller into an error. The backend is applied once at
    construction; the run path must tolerate a forwarded value.
    """

    @pytest.mark.asyncio
    async def test_aprepare_does_not_raise_on_forwarded_storage_backend(self) -> None:
        """A `storage_backend` forwarded into the run path must no longer raise."""
        executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)

        with (
            patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn,
            patch.object(executor, "_load_flow_for_workflow", return_value="flow-name"),
            patch.object(executor, "_set_input_for_flow", new=AsyncMock()),
        ):
            mock_gn.EventManager.return_value.initialize_queue = MagicMock()

            # storage_backend arrives via **kwargs, exactly like the generated double-splat.
            flow_name = await executor.aprepare_workflow_for_run(
                flow_input={},
                storage_backend=StorageBackend.GTC,
            )

        assert flow_name == "flow-name"

    @pytest.mark.asyncio
    async def test_arun_tolerates_forwarded_storage_backend(self) -> None:
        """`arun` must accept a forwarded storage_backend (base-class run path) and ignore it."""
        executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)

        mock_start_result = MagicMock()
        mock_start_result.failed.return_value = True  # short-circuit before the event loop
        mock_prepare = AsyncMock(return_value="flow-name")

        with (
            patch.object(executor, "aprepare_workflow_for_run", new=mock_prepare),
            patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn,
        ):
            mock_gn.ahandle_request = AsyncMock(return_value=mock_start_result)

            # Reaching LocalExecutorError (not ValueError about deprecation) proves the
            # forwarded storage_backend was tolerated and arun proceeded to start the flow.
            with pytest.raises(LocalExecutorError, match="Failed to start flow"):
                await executor.arun(flow_input={}, storage_backend=StorageBackend.GTC)

        # arun must NOT forward storage_backend down to aprepare_workflow_for_run, in any
        # form. Assert it appears in neither the positional args nor the keyword args of the
        # call (asserting only `not in kwargs` would be tautological, since storage_backend
        # is a named parameter of arun and can never land in arun's own **kwargs).
        mock_prepare.assert_awaited_once()
        await_args = mock_prepare.await_args
        assert await_args is not None
        assert StorageBackend.GTC not in await_args.args
        assert "storage_backend" not in await_args.kwargs


class TestSetStorageBackend:
    """The executor applies a storage backend through the environment, never the user's config file."""

    @pytest.fixture(autouse=True)
    def _clear_storage_backend_env_var(self) -> Iterator[None]:
        # patch.dict restores os.environ afterwards, removing the variable the executor sets.
        # monkeypatch.delenv would not: it records nothing to undo when the variable is absent.
        with patch.dict(os.environ):
            os.environ.pop(STORAGE_BACKEND_ENV_VAR, None)
            yield

    def test_explicit_backend_sets_env_var(self) -> None:
        with patch(f"{MODULE_PATH}.has_current_engine", return_value=False):
            LocalWorkflowExecutor(storage_backend=StorageBackend.GTC)

        assert os.environ[STORAGE_BACKEND_ENV_VAR] == StorageBackend.GTC.value

    def test_no_backend_leaves_env_var_unset(self) -> None:
        with patch(f"{MODULE_PATH}.has_current_engine", return_value=False):
            LocalWorkflowExecutor()

        assert STORAGE_BACKEND_ENV_VAR not in os.environ

    def test_no_backend_keeps_a_user_set_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STORAGE_BACKEND_ENV_VAR, StorageBackend.GTC.value)

        with patch(f"{MODULE_PATH}.has_current_engine", return_value=False):
            LocalWorkflowExecutor()

        assert os.environ[STORAGE_BACKEND_ENV_VAR] == StorageBackend.GTC.value

    def test_backend_is_ignored_once_an_engine_exists(self, caplog: pytest.LogCaptureFixture) -> None:
        # The running engine already picked its storage driver; setting the variable now would
        # only reach workers it spawns later, putting them on a different backend.
        with patch(f"{MODULE_PATH}.has_current_engine", return_value=True):
            LocalWorkflowExecutor(storage_backend=StorageBackend.GTC)

        assert STORAGE_BACKEND_ENV_VAR not in os.environ
        assert "Ignored because the engine had already started" in caplog.text


class TestLocalWorkflowExecutorCli:
    """Tests for LocalWorkflowExecutor's CLI surface (issue #4599)."""

    def test_add_cli_arguments_includes_base_flags(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        # Base-class flags carry through.
        assert args.storage_backend is None
        assert args.project_file_path is None

    def test_save_on_failure_absent_is_none(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        assert args.save_on_failure is None

    def test_save_on_failure_bare_flag_is_empty_string(self) -> None:
        # `--save-on-failure` with no value should hit the `const=""` default,
        # which downstream treats as "use the project's save_failed_workflow situation".
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--save-on-failure"])

        assert args.save_on_failure == ""

    def test_save_on_failure_with_value(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--save-on-failure", "/var/dump.py"])

        assert args.save_on_failure == "/var/dump.py"

    def test_cli_constructor_kwargs_maps_save_on_failure_to_save_on_failure_path(self) -> None:
        # Inspect the constructor kwargs derived from CLI args directly, since the
        # constructor itself reaches into ConfigManager which is awkward to set up here.
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--save-on-failure", "/var/dump.py"])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["save_on_failure_path"] == "/var/dump.py"

    def test_cli_constructor_kwargs_storage_backend_default_is_none(self) -> None:
        # None keeps the configured storage backend instead of forcing local.
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args([])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["storage_backend"] is None

    def test_cli_constructor_kwargs_with_gtc_storage_backend(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--storage-backend", StorageBackend.GTC.value])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["storage_backend"] == StorageBackend.GTC

    def test_cli_constructor_kwargs_project_file_path_is_none_when_omitted(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args([])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["project_file_path"] is None

    def test_cli_constructor_kwargs_project_file_path_converted_to_path(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--project-file-path", "/some/project.yaml"])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["project_file_path"] == Path("/some/project.yaml")

    def test_deprecated_pickle_flag_still_parses_and_is_ignored(self) -> None:
        # Workflow files saved by earlier engines seed the default and callers may pass the flag.
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser, pickle_control_flow_result_default=True)
        args = parser.parse_args(["--pickle-control-flow-result"])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert "pickle_control_flow_result" not in kwargs
        LocalWorkflowExecutor(pickle_control_flow_result=True)


class TestSetInputForFlow:
    @pytest.mark.asyncio
    async def test_tagged_input_values_are_decoded(self) -> None:
        """Inputs sent as encoded values, as a subprocess run's are, reach the start node as values."""
        executor = LocalWorkflowExecutor.__new__(LocalWorkflowExecutor)
        start_node = MagicMock(spec=StartNode)
        flow = MagicMock()
        flow.nodes = {"Start": start_node}
        set_result = MagicMock()
        set_result.failed.return_value = False

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.FlowManager.return_value.get_flow_by_name.return_value = flow
            mock_gn.ahandle_request = AsyncMock(return_value=set_result)
            await executor._set_input_for_flow(
                flow_name="flow", flow_input={"Start": {"pair": encode_value((1, 2)), "text": "plain"}}
            )

        values = {call.args[0].parameter_name: call.args[0].value for call in mock_gn.ahandle_request.await_args_list}
        assert values == {"pair": (1, 2), "text": "plain"}
