from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem


@dataclass
class LibraryNotRegisteredProblem(WorkflowProblem):
    """Problem indicating a required library was not successfully registered.

    This is stackable - multiple libraries can fail to register.

    `reason` carries why registration failed, when the recorder knows. A library that is
    present on disk but switched off in `libraries_to_register` is the case that most needs
    it: without the reason the reader goes looking for something that is already there.
    Callers that only observed the absence (the metadata dependency check, which reads the
    registered-library list rather than attempting a registration) leave it None.
    """

    library_name: str
    reason: str | None = None

    @classmethod
    def collate_problems_for_display(cls, instances: list[LibraryNotRegisteredProblem]) -> str:
        """Display library not registered problems.

        Sorts by library_name and lists all affected libraries.
        """
        if len(instances) == 1:
            problem = instances[0]
            return f"'{problem.library_name}' not registered. {problem._reason_or_default()}"

        # Sort by library_name
        sorted_instances = sorted(instances, key=lambda p: p.library_name)

        output_lines = []
        output_lines.append(f"{len(instances)} libraries not registered:")
        for i, problem in enumerate(sorted_instances, 1):
            output_lines.append(f"  {i}. {problem.library_name}: {problem._reason_or_default()}")

        return "\n".join(output_lines)

    def _reason_or_default(self) -> str:
        """The recorded reason, or the generic stand-in when none was captured."""
        if self.reason is None:
            return "May have other problems preventing load."
        return self.reason
