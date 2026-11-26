"""Provide a scope for running a nested workflow.

Entering the scope clears engine state that belongs only to the enclosing workflow, such as a loop's node
name mappings, and restores it afterwards.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from griptape_nodes.common.loop_body_scope import current_loop_body_scope

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def entered_nested_workflow() -> Iterator[None]:
    """Enter a scope for running a nested workflow, without the state that belongs only to the enclosing workflow.

    The loop body scope is cleared: the workflow behind a Workflow node runs in its own subflow, whose
    nodes are named as they are there, so an enclosing loop's node-name mappings do not apply to them.
    """
    token = current_loop_body_scope.set(None)
    try:
        yield
    finally:
        current_loop_body_scope.reset(token)
