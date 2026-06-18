from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass
class PermissionDeniedLibraryProblem(LibraryProblem):
    """Problem indicating a pre-dispatch hook forbids loading this library.

    Raised when the engine screens the library's fitness request through the
    registered hook chain and a hook short-circuits it (in practice the host
    application's license policy). The library is treated as unusable and is not
    registered. `detail` carries the hook's own explanation -- the missing
    capability and what to ask an administrator for -- so the GUI can surface it
    on the library's failure icon.

    Stackable: a library screened more than once (e.g. re-evaluated on reload)
    can accumulate several detail lines.
    """

    detail: str

    @classmethod
    def collate_problems_for_display(cls, instances: list[PermissionDeniedLibraryProblem]) -> str:
        """Display the explanation(s) for why loading the library was denied."""
        details = [instance.detail for instance in instances if instance.detail]
        if not details:
            return "Loading this library is not permitted by the active policy."
        if len(details) == 1:
            return details[0]
        return "Loading this library is not permitted by the active policy:\n" + "\n".join(
            f"  - {detail}" for detail in details
        )
