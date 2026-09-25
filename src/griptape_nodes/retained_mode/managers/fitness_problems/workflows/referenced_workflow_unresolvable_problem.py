from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem


@dataclass
class ReferencedWorkflowUnresolvableProblem(WorkflowProblem):
    """Problem indicating a workflow this one references could not be read.

    This is stackable - a workflow can reference several sub-workflows.

    The dependency check walks the referenced workflows to collect the libraries they need, so a
    referenced workflow it cannot read leaves a hole in that collection: the check reports on the
    libraries it did find and has no way to know what the unreadable one would have added. That is
    worth saying out loud rather than passing over, because the libraries behind the hole are
    exactly the ones whose absence would otherwise surface later as a node that will not construct.

    `reason` carries why it could not be read - not registered, unsaved, or a malformed metadata
    header.
    """

    workflow_name: str
    reason: str | None = None

    @classmethod
    def collate_problems_for_display(cls, instances: list[ReferencedWorkflowUnresolvableProblem]) -> str:
        """Display unresolvable referenced workflow problems.

        Sorts by workflow_name and lists all affected workflows.
        """
        if len(instances) == 1:
            problem = instances[0]
            suffix = f": {problem.reason}" if problem.reason else ""
            return (
                f"Referenced workflow '{problem.workflow_name}' could not be read{suffix}. "
                "The libraries it needs are not included in this workflow's dependency check."
            )

        sorted_instances = sorted(instances, key=lambda p: p.workflow_name)
        header = (
            f"{len(instances)} referenced workflows could not be read. The libraries they need are "
            "not included in this workflow's dependency check:"
        )
        rows = []
        for i, problem in enumerate(sorted_instances, 1):
            suffix = f": {problem.reason}" if problem.reason else ""
            rows.append(f"  {i}. {problem.workflow_name}{suffix}")

        return "\n".join([header, *rows])
