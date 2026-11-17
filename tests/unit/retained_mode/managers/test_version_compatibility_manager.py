"""Tests for VersionCompatibilityManager's workflow compatibility checks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.node_library.workflow_registry import (
    LibraryNameAndNodeType,
    LibraryNameAndVersion,
    WorkflowMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultFailure,
    ListRegisteredLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import NodeTypeNotFoundProblem

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


def _register_library_with_no_node_types(name: str) -> None:
    """Register a library the engine can resolve, holding no node types at all.

    A library absent from the registry is skipped by every branch of the deprecated-node
    check, so a test using one cannot tell a check that ran from one that returned early.
    Registering it empty means a check that does run reaches the node lookup and misses.

    Args:
        name: The library name, as declared in the workflow's referenced libraries.
    """
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="t",
            description="d",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    LibraryRegistry.generate_new_library(library_data=schema)


def _workflow_metadata_using(library_name: str, node_type: str) -> WorkflowMetadata:
    """Build metadata for a workflow that uses one node type from one library."""
    return WorkflowMetadata(
        name="uses_a_library",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[LibraryNameAndVersion(library_name=library_name, library_version="1.0.0")],
        node_types_used={LibraryNameAndNodeType(library_name=library_name, node_type=node_type)},
    )


class TestCheckWorkflowVersionCompatibility:
    """The deprecated-node check's two ways of learning which libraries are registered."""

    @pytest.fixture(autouse=True)
    def _clean_library_registry(self) -> Generator[None, None, None]:
        """Drop registered libraries around each test, since the registry is process-global."""
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.mark.asyncio
    async def test_fetches_the_library_list_when_none_is_supplied(self, engine: Engine) -> None:
        """A caller that has no library list in hand gets one fetched for it.

        This is the single-workflow path. Only the bulk registration scan has already
        fetched the list, so every other caller depends on this fetch happening.
        """
        metadata = _workflow_metadata_using("Nowhere Library", "SomeNode")
        original_ahandle = engine.ahandle_request
        list_libraries_call_count = 0

        async def counting_ahandle_request(request: RequestPayload) -> ResultPayload:
            nonlocal list_libraries_call_count
            if isinstance(request, ListRegisteredLibrariesRequest):
                list_libraries_call_count += 1
                return ListRegisteredLibrariesResultSuccess(libraries=["Nowhere Library"], result_details="ok")
            return await original_ahandle(request)

        with patch.object(engine, "ahandle_request", side_effect=counting_ahandle_request):
            await engine.version_compatibility_manager.check_workflow_version_compatibility(metadata)

        assert list_libraries_call_count == 1

    @pytest.mark.asyncio
    async def test_reports_no_issues_when_the_library_list_cannot_be_fetched(self, engine: Engine) -> None:
        """A failed fetch leaves the deprecated-node check with nothing to say.

        Without the registered-library list there is no way to tell a deprecated node from a
        node whose library simply is not installed, so the check stays silent rather than
        warning about nodes it cannot look up.

        The library here is registered but does not have the node type, so a check that
        carried on without the list would reach the node lookup and emit
        NodeTypeNotFoundProblem. Silence is therefore the early return, not the library
        being unresolvable.
        """
        _register_library_with_no_node_types("Resolvable Library")
        metadata = _workflow_metadata_using("Resolvable Library", "SomeNode")
        original_ahandle = engine.ahandle_request

        async def failing_list_libraries(request: RequestPayload) -> ResultPayload:
            if isinstance(request, ListRegisteredLibrariesRequest):
                return ListRegisteredLibrariesResultFailure(result_details="Registry unavailable")
            return await original_ahandle(request)

        with patch.object(engine, "ahandle_request", side_effect=failing_list_libraries):
            issues = await engine.version_compatibility_manager.check_workflow_version_compatibility(metadata)

        assert issues == []

    @pytest.mark.asyncio
    async def test_reports_a_missing_node_type_when_the_library_list_arrives(self, engine: Engine) -> None:
        """With the list in hand, the check does look nodes up, and says so when one is gone.

        The positive control for the fetch-failure case above: same library, same node type,
        the only difference being that the check gets the list it needs.
        """
        _register_library_with_no_node_types("Resolvable Library")
        metadata = _workflow_metadata_using("Resolvable Library", "SomeNode")

        issues = await engine.version_compatibility_manager.check_workflow_version_compatibility(
            metadata, registered_libraries=["Resolvable Library"]
        )

        assert [type(issue.problem) for issue in issues] == [NodeTypeNotFoundProblem]

    @pytest.mark.asyncio
    async def test_uses_a_supplied_library_list_without_fetching(self, engine: Engine) -> None:
        """A caller that already has the list is not made to pay for a second fetch.

        This is the whole reason the argument exists: the registration scan fetches the list
        once and passes it down, instead of firing one request per workflow it scans.
        """
        metadata = _workflow_metadata_using("Nowhere Library", "SomeNode")
        original_ahandle = engine.ahandle_request
        list_libraries_call_count = 0

        async def counting_ahandle_request(request: RequestPayload) -> ResultPayload:
            nonlocal list_libraries_call_count
            if isinstance(request, ListRegisteredLibrariesRequest):
                list_libraries_call_count += 1
            return await original_ahandle(request)

        with patch.object(engine, "ahandle_request", side_effect=counting_ahandle_request):
            await engine.version_compatibility_manager.check_workflow_version_compatibility(
                metadata, registered_libraries=["Nowhere Library"]
            )

        assert list_libraries_call_count == 0
