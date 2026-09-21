from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.node_library.workflow_registry import WorkflowMetadataError, read_workflow_metadata
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import (
    ReferencedWorkflowUnresolvableProblem,
)

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
    from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, _WorkflowRegistry
    from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem


class ReferencedWorkflowDependencies(NamedTuple):
    """What a walk of a workflow's referenced sub-workflows found.

    `libraries` are the libraries those sub-workflows declare, deduplicated by name.
    `problems` name the referenced workflows the walk could not read, so a caller can say which
    part of the picture is missing instead of presenting a partial one as complete.
    """

    libraries: list[LibraryNameAndVersion]
    problems: list[WorkflowProblem]


def collect_referenced_workflow_dependencies(
    workflow_registry: _WorkflowRegistry, workflow_metadata: WorkflowMetadata
) -> ReferencedWorkflowDependencies:
    """Walk the workflows `workflow_metadata` references and collect the libraries they declare.

    A workflow's own header records only the libraries its own nodes need, so the libraries
    behind a referenced sub-workflow have to be gathered from that sub-workflow's header at the
    moment they are needed. Reading them here rather than trusting a copy taken when the
    referencing workflow was saved is the point: the sub-workflow can be edited afterwards, and
    a copy would still describe the libraries it used to need.

    The walk is recursive (a referenced workflow may reference others) and cycle-safe: a
    workflow that references one already visited contributes nothing further. Cycles are
    reachable in ordinary use -- two workflows that each carry a node backed by the other --
    and are not themselves a problem to report, since the check only needs each workflow's
    libraries once.

    A referenced workflow that cannot be read yields a ReferencedWorkflowUnresolvableProblem
    rather than stopping the walk, so one unreadable sub-workflow does not cost the caller the
    libraries of its siblings. Returns the libraries found and those problems; the caller
    decides what they mean for fitness.
    """
    libraries: dict[str, LibraryNameAndVersion] = {}
    problems: list[WorkflowProblem] = []
    visited: set[str] = set()
    queue: list[str] = list(workflow_metadata.workflows_referenced or [])

    while queue:
        workflow_name = queue.pop(0)
        if workflow_name in visited:
            continue
        visited.add(workflow_name)

        referenced_metadata = _read_referenced_workflow_metadata(workflow_registry, workflow_name, problems)
        if referenced_metadata is None:
            continue

        for lib_ref in referenced_metadata.node_libraries_referenced:
            # First spelling of a library name wins. Two sub-workflows can name the same library
            # at different versions, and choosing between them is the version check's job, not
            # this walk's -- it reports on what the workflow closest to the root asked for.
            if lib_ref.library_name not in libraries:
                libraries[lib_ref.library_name] = lib_ref
        queue.extend(referenced_metadata.workflows_referenced or [])

    return ReferencedWorkflowDependencies(libraries=list(libraries.values()), problems=problems)


def _read_referenced_workflow_metadata(
    workflow_registry: _WorkflowRegistry, workflow_name: str, problems: list[WorkflowProblem]
) -> WorkflowMetadata | None:
    """Read one referenced workflow's metadata header, recording a problem if it cannot be read.

    Goes to the file rather than to the registry entry's cached metadata, because that entry is
    only as current as the last time it was registered and this walk exists to see the header as
    it is now. The registry is still what maps the referenced name to a path.
    """
    if not workflow_registry.has_workflow_with_name(workflow_name):
        problems.append(
            ReferencedWorkflowUnresolvableProblem(
                workflow_name=workflow_name, reason="not registered in this workspace"
            )
        )
        return None

    referenced_workflow = workflow_registry.get_workflow_by_name(workflow_name)
    if referenced_workflow.file_path is None:
        problems.append(
            ReferencedWorkflowUnresolvableProblem(workflow_name=workflow_name, reason="has never been saved")
        )
        return None

    complete_path = Path(workflow_registry.get_complete_file_path(referenced_workflow.file_path))
    try:
        return read_workflow_metadata(complete_path)
    except WorkflowMetadataError as err:
        problems.append(ReferencedWorkflowUnresolvableProblem(workflow_name=workflow_name, reason=str(err)))
        return None
