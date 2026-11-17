from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class LibraryNotProvidedByEnvironmentProblem(LibraryProblem):
    """Problem indicating a configured library was not loaded because the environment does not provide it.

    Raised when `library.provisioned_by` is 'environment': the engine then loads only the
    libraries listed in `GTN_LIBRARY_PATHS`, and every other configured library (a
    `libraries_to_register` entry, the sandbox library) gets this problem instead of loading.
    """

    library_path: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[LibraryNotProvidedByEnvironmentProblem]) -> str:  # noqa: ARG003 (one message whatever the count; each LibraryInfo has one path)
        return (
            "This library was not loaded because the engine is running in an environment that provides "
            "its libraries, and this library is not one of them. Ask whoever set up this environment to "
            "add it, or remove it from your library settings."
        )
