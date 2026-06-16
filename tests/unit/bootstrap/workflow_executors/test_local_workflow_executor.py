"""Unit tests for LocalWorkflowExecutor._load_project."""

from argparse import ArgumentParser
from pathlib import Path, PureWindowsPath
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
    LocalExecutorError,
    LocalWorkflowExecutor,
)
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectResultSuccess,
)

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
        executor._pickle_control_flow_result = False

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


class TestLocalWorkflowExecutorCli:
    """Tests for LocalWorkflowExecutor's CLI surface (issue #4599)."""

    def test_add_cli_arguments_includes_base_flags(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        # Base-class flags carry through.
        assert args.storage_backend == StorageBackend.LOCAL.value
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

    def test_cli_constructor_kwargs_storage_backend_default_is_local_enum(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args([])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["storage_backend"] == StorageBackend.LOCAL

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

    def test_cli_constructor_kwargs_pickle_inherits_argparse_default(self) -> None:
        # When `add_cli_arguments` was seeded with the save-time default, that
        # value flows through `_cli_constructor_kwargs` into the constructor.
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser, pickle_control_flow_result_default=True)
        args = parser.parse_args([])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["pickle_control_flow_result"] is True

    def test_cli_constructor_kwargs_pickle_flag_overrides_seeded_default(self) -> None:
        parser = ArgumentParser()
        LocalWorkflowExecutor.add_cli_arguments(parser, pickle_control_flow_result_default=False)
        args = parser.parse_args(["--pickle-control-flow-result"])

        kwargs = LocalWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["pickle_control_flow_result"] is True
