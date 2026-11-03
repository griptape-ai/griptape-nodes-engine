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
            "aprocess. In a worker, each execution runs on a copy "
            "rebuilt from the orchestrator's node, so a direct mutation "
            "during aprocess is local to that copy and never syncs. The "
            "request-driven path syncs to the orchestrator's node, but "
            "is not readable back on the executing copy until the next "
            "execution rebuilds it."
        ),
        remediation_template=(
            "Node '{node_name}' (type '{node_class}') mutated parameter "
            "'{parameter_name}' during aprocess via {mutation}. Emit "
            "AddParameterToNodeRequest or RemoveParameterFromNodeRequest "
            "to propagate the change to the orchestrator. Note that the "
            "change reaches the orchestrator's node, not this one: do "
            "not read the parameter back locally. Each execution rebuilds "
            "its copy from the orchestrator's node, so the parameter "
            "exists here from the next execution on."
        ),
    ),
    "graph-access-while-loading": StrictModeRule(
        rule_id="graph-access-while-loading",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node sent a request that reads or changes connections, its "
            "flow, or another node while it was being created or having "
            "its saved state applied: in before_value_set or "
            "set_parameter_value with initial_setup, or as its creation "
            "set its state. The graph around a "
            "loading node is incomplete -- a workflow load makes "
            "connections before values, a paste after them, and a "
            "worker's copy of the node has no connections or other nodes "
            "at all -- so the answer depends on how the node was loaded."
        ),
        remediation_template=(
            "Node '{node_name}' sent {request_type} while it was loading. "
            "While initial_setup is set, change the node itself (its "
            "parameters, types and visibility) and leave connections and "
            "other nodes alone: the loader makes the connections. Do the "
            "graph work when the value is edited instead."
        ),
    ),
    "state-read-bypassing-requests": StrictModeRule(
        rule_id="state-read-bypassing-requests",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node read engine state through a manager while it ran, "
            "instead of asking with a request. In a worker the manager "
            "holds that process's own copy of the state, not the "
            "orchestrator's, so the answer would be wrong; a request is "
            "answered by whichever process owns the state."
        ),
        remediation_template=(
            "Node '{node_name}' used {manager_name} while it ran. Use "
            "GriptapeNodes.handle_request({request_hint}) instead, which "
            "gives the same answer in-process and in a worker."
        ),
    ),
    "state-outside-parameters": StrictModeRule(
        rule_id="state-outside-parameters",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node's after_value_set or connection hook set an attribute "
            "on the node. after_value_set doesn't run when a node loads, "
            "so a reopened workflow never has that attribute, and neither "
            "hook runs for the copy a worker builds to run the node, which "
            "has no connections: anything that reads the attribute "
            "behaves differently there."
        ),
        remediation_template=(
            "Node '{node_name}' set attribute(s) {attribute_names} in "
            "{hook_name}. Keep that state in a parameter (hidden if it "
            "isn't for the user), or derive it from the node's values "
            "where it is needed."
        ),
    ),
}
