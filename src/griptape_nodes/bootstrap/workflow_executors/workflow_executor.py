import asyncio
import logging
from abc import abstractmethod
from argparse import ArgumentParser, Namespace
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from griptape_nodes.drivers.storage import StorageBackend

logger = logging.getLogger(__name__)


class WorkflowExecutor:
    def __init__(self, *, pickle_control_flow_result: bool = False) -> None:  # noqa: ARG002
        """``pickle_control_flow_result`` is deprecated and ignored; saved workflow files still pass it."""
        self.output: dict | None = None

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit."""
        return

    def run(
        self,
        flow_input: Any,
        storage_backend: StorageBackend = StorageBackend.LOCAL,
        **kwargs: Any,
    ) -> None:
        return asyncio.run(self.arun(flow_input, storage_backend, **kwargs))

    @abstractmethod
    async def arun(
        self,
        flow_input: Any,
        storage_backend: StorageBackend = StorageBackend.LOCAL,
        **kwargs: Any,
    ) -> None: ...

    @classmethod
    def add_cli_arguments(
        cls,
        parser: ArgumentParser,
        *,
        pickle_control_flow_result_default: bool = False,
    ) -> None:
        """Register executor-level CLI arguments on `parser`.

        Subclasses override and call `super().add_cli_arguments(parser, ...)` to
        inherit the shared base-class flags before adding their own. Subclasses
        whose constructor cannot accept a particular flag should compose the
        smaller `_add_*_argument` helpers directly instead of calling super.

        `pickle_control_flow_result_default` is deprecated and ignored; workflow files saved by
        earlier engines still pass it.
        """
        cls._add_storage_backend_argument(parser)
        cls._add_project_file_path_argument(parser)
        cls._add_pickle_control_flow_result_argument(parser, default=pickle_control_flow_result_default)

    @classmethod
    def _add_storage_backend_argument(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--storage-backend",
            choices=[StorageBackend.LOCAL.value, StorageBackend.GTC.value],
            default=StorageBackend.LOCAL.value,
            help="Storage backend to use: 'local' for local filesystem or 'gtc' for Griptape Cloud",
        )

    @classmethod
    def _add_project_file_path_argument(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--project-file-path",
            default=None,
            help="Path to a project file to load for the workflow execution",
        )

    @classmethod
    def _add_pickle_control_flow_result_argument(cls, parser: ArgumentParser, *, default: bool = False) -> None:
        # Kept so command lines that still pass the flag keep parsing.
        parser.add_argument(
            "--pickle-control-flow-result",
            action="store_true",
            default=default,
            help="Deprecated and ignored. Flow results always travel as plain data.",
        )

    @classmethod
    def from_cli_args(cls, args: Namespace, **overrides: Any) -> Self:
        """Construct an executor from a parsed argparse `Namespace`.

        Subclasses override to map their own CLI arguments to constructor kwargs.
        `**overrides` lets callers (e.g. the generated workflow file's __main__)
        inject non-CLI constructor kwargs like `skip_library_loading` or
        `workflows_to_register` that aren't exposed as flags.
        """
        kwargs = cls._cli_constructor_kwargs(args)
        kwargs.update(overrides)
        return cls(**kwargs)

    @classmethod
    def _cli_constructor_kwargs(cls, args: Namespace) -> dict[str, Any]:
        """Return constructor kwargs derived from CLI args.

        Subclasses override to extend the mapping; they should call
        `super()._cli_constructor_kwargs(args)` first and then update the dict
        with their own entries.
        """
        return {
            "storage_backend": StorageBackend(args.storage_backend),
            "project_file_path": Path(args.project_file_path) if args.project_file_path is not None else None,
        }
