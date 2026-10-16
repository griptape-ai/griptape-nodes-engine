"""Where the nodes of a Private Execution run go, recorded where that is decided.

`NodeExecutor` decides that a group or loop runs with Private Execution. With the
`private_execution_in_worker` beta feature on, it runs the group's nodes through the local path
inside `private_execution_scope`, and `NodeManager` sends each node it executes to the worker the
scope names. The scope is a ContextVar, so it reaches every node the group runs: the tasks that
run them are created inside it and copy it.

Kept in its own module because both `NodeExecutor` and `NodeManager` read it, and neither may
import the other.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class PrivateExecutionScope:
    """The node that set Private Execution, and the worker its nodes run on.

    Args:
        owner_name: The group or loop whose `execution_environment` is Private Execution.
        worker_key: The worker the nodes run on. None is the general-purpose worker that stays up
            for the session, which every Private Execution run shares.
    """

    owner_name: str
    worker_key: str | None = None


current_private_execution_scope: ContextVar[PrivateExecutionScope | None] = ContextVar(
    "current_private_execution_scope", default=None
)


@contextmanager
def private_execution_scope(owner_name: str) -> Iterator[None]:
    """Send the nodes run inside this block to the general-purpose worker.

    The innermost scope wins, so a Private Execution group nested in another names itself.
    """
    token = current_private_execution_scope.set(PrivateExecutionScope(owner_name=owner_name))
    try:
        yield
    finally:
        current_private_execution_scope.reset(token)
