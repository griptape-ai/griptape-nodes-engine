"""Strict-mode rule registry.

Central catalog of every strict-mode rule: its stable ``rule_id``,
default severity, whether it is a correctness-class violation (failed
even on the orchestrator) or an ergonomics-class warning (worker-only
escalation), a human description, and a ``str.format``-ready
remediation template.

Detectors import ``RULES`` to look up their rule and call
``STRICT_MODE.report(rule_id=..., message=RULES[rid].render(...))``
at their own call site. No enforcement logic lives here -- this
module is a static catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from griptape_nodes.common.strict_mode import StrictModeSeverity


@dataclass(frozen=True)
class StrictModeRule:
    """Static description of a single strict-mode rule.

    ``correctness`` rules are rules whose violation means the system is
    in a state that cannot produce correct results (deadlocks, lost
    data, state that silently disagrees between orchestrator and
    worker). These fail on both sides. ``correctness=False`` rules
    describe ergonomics or API-shape issues where the system still
    runs -- they warn on the orchestrator and escalate to a failure on
    the worker because the worker's stateless model makes them
    load-bearing.
    """

    rule_id: str
    default_severity: StrictModeSeverity
    correctness: bool
    description: str
    remediation_template: str
    worker_escalation: bool = True

    def render(self, **context: Any) -> str:
        return self.remediation_template.format(**context)


RULES: dict[str, StrictModeRule] = {
    "parameter-mutation-during-aprocess": StrictModeRule(
        rule_id="parameter-mutation-during-aprocess",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node called add_parameter or remove_parameter during "
            "aprocess, which violates the structure contract: a "
            "node's parameter structure must be a deterministic "
            "function of its parameter values, created in __init__ or "
            "by a value hook. Structure created anywhere else cannot "
            "survive, because each execution builds a fresh copy from "
            "the node class and only VALUES carry over (hydration "
            "re-runs the hooks, which is how derived structure "
            "reappears). A direct mutation during aprocess is local "
            "to the transient copy and never syncs; the request-driven "
            "path syncs to the orchestrator but is not readable back "
            "on the executing copy, and does not reappear on later "
            "executions either."
        ),
        remediation_template=(
            "Node '{node_name}' (type '{node_class}') mutated parameter "
            "'{parameter_name}' during aprocess via {mutation}. Emit "
            "AddParameterToNodeRequest or RemoveParameterFromNodeRequest "
            "to propagate the change to the orchestrator. Note that the "
            "change reaches the orchestrator's node, not this one: do "
            "not read the parameter back locally. Each execution builds "
            "a fresh copy from the node class, so the parameter exists "
            "here only if __init__ or a value hook re-creates it."
        ),
    ),
}
