"""One engine's workflow context, in a form another engine can adopt.

No worker-side path pushes the workflow stack, so a worker answers "there isn't one" to every
workflow question, and it does so silently: `workflow_dir` raises, the default `{outputs}` situation
marks the reference optional (`{workflow_dir?:/}outputs`), the raise is swallowed, and the path
degrades to workspace-relative. The worker then writes real files where the orchestrator does not
read, with no error at the divergence.

The orchestrator lends its CONTEXT rather than any value derived from it, so everything derived is
answered by the adopting engine's normal code paths and no new handoff is needed per value.
"""

from __future__ import annotations

from typing import NamedTuple


class WorkflowContextSnapshot(NamedTuple):
    """A workflow context in the three fields `ContextManager.WorkflowContextState` keeps.

    `name` None means the lending engine had no current workflow, and the adopting engine must then
    have none either. Both engines degrading a path the same way is correct; disagreeing is the bug
    this exists to prevent.
    """

    name: str | None = None
    file_path: str | None = None
    working_directory: str | None = None
