"""Unit tests for LocalSessionWorkflowExecutor's CLI surface (issue #4599)."""

from argparse import ArgumentParser
from pathlib import Path

from griptape_nodes.bootstrap.workflow_executors.local_session_workflow_executor import (
    LocalSessionWorkflowExecutor,
)
from griptape_nodes.drivers.storage import StorageBackend


class TestLocalSessionWorkflowExecutorCli:
    """Tests for LocalSessionWorkflowExecutor's CLI surface."""

    def test_add_cli_arguments_includes_storage_backend(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        assert args.storage_backend == StorageBackend.LOCAL.value

    def test_add_cli_arguments_includes_save_on_failure(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--save-on-failure", "/var/dump.py"])

        assert args.save_on_failure == "/var/dump.py"

    def test_add_cli_arguments_includes_session_id(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--session-id", "abc-123"])

        assert args.session_id == "abc-123"

    def test_add_cli_arguments_session_id_defaults_to_none(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        assert args.session_id is None

    def test_cli_constructor_kwargs_includes_events_url(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--events-url", "ws://127.0.0.1:1234/"])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["events_url"] == "ws://127.0.0.1:1234/"

    def test_add_cli_arguments_includes_project_file_path(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--project-file-path", "/some/project.yaml"])

        assert args.project_file_path == "/some/project.yaml"

    def test_cli_constructor_kwargs_includes_session_id(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--session-id", "abc-123"])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["session_id"] == "abc-123"

    def test_cli_constructor_kwargs_project_file_path_converted_to_path(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--project-file-path", "/some/project.yaml"])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["project_file_path"] == Path("/some/project.yaml")

    def test_cli_constructor_kwargs_storage_backend_converted_to_enum(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--storage-backend", StorageBackend.GTC.value])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["storage_backend"] == StorageBackend.GTC
