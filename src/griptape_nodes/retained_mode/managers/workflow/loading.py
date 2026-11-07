from __future__ import annotations

import contextvars
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from types import TracebackType

    from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem

# Stands in for a creation or modification date the workflow metadata lacks.
EPOCH_START = datetime(tzinfo=UTC, year=1970, month=1, day=1)

# LoadProblemFrame writes this; is_loading_workflow reads it. Scoped to the
# current context so concurrent loads cannot pop or read each other's frames.
_load_problem_frames: contextvars.ContextVar[tuple[list[WorkflowProblem], ...]] = contextvars.ContextVar(
    "workflow_load_problem_frames", default=()
)


class LoadProblemFrame:
    """Collects one workflow load's problems, bubbling them into the enclosing load on exit.

    A workflow file can import another workflow as a referenced subflow, and that import
    runs as a nested request dispatched from inside the outer file's exec() -- so the inner
    load's problems have no return path to the outer one. Without bubbling, an outer load
    reports GOOD while the canvas holds the inner load's placeholders, and a caller that
    gates on status (the headless executor) runs an incomplete graph.

    The stack lives in a ContextVar rather than on the manager because loads genuinely run
    concurrently: in PARALLEL execution mode a WorkflowNode loads its subflow from inside a
    node body, and those bodies run as separate tasks. A shared list would let one task pop
    another's frame, so a load would report a library a *different* workflow was missing --
    or see a sibling's open frame and suppress the only report of its own. A task inherits a
    copy of the context, so a nested load still reaches the enclosing frame (same task) while
    siblings stay isolated. `EventSuppressionContext` is contextvar-scoped for the same reason.
    """

    def __init__(self) -> None:
        self.problems: list[WorkflowProblem] = []
        self._tokens: list[contextvars.Token[tuple[list[WorkflowProblem], ...]]] = []

    def __enter__(self) -> Self:
        self._tokens.append(_load_problem_frames.set((*_load_problem_frames.get(), self.problems)))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        frames = _load_problem_frames.get()
        if len(frames) > 1:
            enclosing = frames[-2]
            # A library the outer file declares for itself AND reaches through a subflow
            # would otherwise be counted twice, and the collated display would name it
            # twice while claiming two libraries are missing.
            enclosing.extend(problem for problem in self.problems if problem not in enclosing)
        if self._tokens:
            _load_problem_frames.reset(self._tokens.pop())


def is_loading_workflow() -> bool:
    """Whether a workflow load is in progress, so its result will report the problems found.

    Read after a nested load has closed, so a frame still on the stack is an ENCLOSING load.
    That makes this the complement of the bubble in LoadProblemFrame.__exit__: exactly one of
    the two names any given problem. A frame that stopped bubbling would have to stop
    answering True here as well, or nothing would report it.
    """
    return len(_load_problem_frames.get()) > 0
