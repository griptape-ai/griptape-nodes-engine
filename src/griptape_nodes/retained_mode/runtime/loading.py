"""What one open workflow is in the middle of loading."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkflowLoading:
    """The referenced workflows one open workflow is importing, innermost last.

    The flows created during an import are tagged with the innermost name.
    """

    referenced_workflow_stack: list[str] = field(default_factory=list)
