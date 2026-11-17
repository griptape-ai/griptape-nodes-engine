"""Whether a parameter is being added or removed by a request rather than by a node's own code."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Per-task flag: when set, parameter mutations on a node are explicitly
# sanctioned by a request handler (AddParameterToNodeRequest /
# RemoveParameterFromNodeRequest) and the parameter-mutation-during-aprocess
# detector should not fire. The handler-side path is the legitimate way to
# mutate parameters because it propagates the change back to the orchestrator
# via the request bus; direct add_parameter / remove_parameter_element calls
# from inside aprocess do not, and that is what the rule catches.
_sanctioned_mutation: ContextVar[bool] = ContextVar("_node_types_sanctioned_mutation", default=False)


@contextmanager
def sanctioned_parameter_mutation() -> Iterator[None]:
    """Mark the enclosed block as a request-driven parameter mutation.

    Wraps add_parameter / remove_parameter_element calls inside
    AddParameterToNodeRequest and RemoveParameterFromNodeRequest handlers
    so the parameter-mutation-during-aprocess detector skips them.
    """
    token = _sanctioned_mutation.set(True)
    try:
        yield
    finally:
        _sanctioned_mutation.reset(token)
