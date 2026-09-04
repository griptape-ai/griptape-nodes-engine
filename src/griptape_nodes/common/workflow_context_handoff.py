"""One engine's workflow context, in a form another engine can adopt.

`ContextManager.push_workflow` is called on the orchestrator's run path and nowhere else, so a
worker has no workflow context and answers "there isn't one" to every workflow question. That is
not loud. `workflow_dir` raises, the default `{outputs}` situation marks the reference optional
(`{workflow_dir?:/}outputs`), the raise is swallowed, and the path degrades from the workflow's own
folder to a workspace-relative one -- the worker writes real files where the orchestrator does not
read, with no error at the divergence, surfacing later as a missing file in a downstream node.

The same gap has produced other defects: `is_variable_substitution_enabled` answers True in a
worker regardless of the workflow's setting, and `variables` had to be precomputed and carried on
the execution request to work around it.

So the orchestrator lends its CONTEXT rather than any value derived from it. Everything derived --
both path builtins, substitution enablement, whatever is added later -- is then answered by the
adopting engine's normal code paths, which is what stops this from needing a new handoff per value.
"""

from __future__ import annotations

from typing import NamedTuple


class WorkflowContextSnapshot(NamedTuple):
    """A workflow context in the three fields `ContextManager.WorkflowContextState` keeps.

    `name` None means the lending engine had no current workflow, so there is nothing to adopt and
    the peer keeps answering from its own equally-empty context. Both engines degrading a path the
    same way is correct; disagreeing is the bug this exists to prevent.
    """

    name: str | None = None
    file_path: str | None = None
    working_directory: str | None = None

    def is_empty(self) -> bool:
        """True when there is no workflow to adopt."""
        return self.name is None
