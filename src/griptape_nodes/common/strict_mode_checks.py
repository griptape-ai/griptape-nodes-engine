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

    ``drops_class_from_schema`` is an independent load-lifecycle signal:
    when a class's schema probe fires such a rule, the class is skipped
    (dropped from the worker schema) during library load. It is distinct
    from severity, which only governs logging and worker-side failure
    promotion. A rule can be an ergonomics warning at execution time yet
    still be load-bearing enough to exclude the class from the worker
    schema (e.g. a bus call in __init__ deadlocks the worker's probe).
    """

    rule_id: str
    default_severity: StrictModeSeverity
    correctness: bool
    description: str
    remediation_template: str
    worker_escalation: bool = True
    drops_class_from_schema: bool = False

    def render(self, **context: Any) -> str:
        return self.remediation_template.format(**context)


RULES: dict[str, StrictModeRule] = {
    "reentrant-bus-in-init": StrictModeRule(
        rule_id="reentrant-bus-in-init",
        default_severity=StrictModeSeverity.WARNING,
        # Ergonomics at execution time: a local node that hits the bus in
        # __init__ still runs, so it warns on the orchestrator and escalates
        # to a failure only on the worker (worker_escalation default). The
        # deadlock hazard is worker-only, so the class is still dropped from
        # the worker schema during library load via drops_class_from_schema.
        correctness=False,
        description=(
            "A node issued an event-bus request from inside its __init__. "
            "The worker library probe runs __init__ to extract a schema; "
            "re-entering the bus there deadlocks the worker."
        ),
        remediation_template=(
            "Issued '{request_type}' during __init__. "
            "Move the call into aprocess (or a lifecycle hook that runs after "
            "construction)."
        ),
        drops_class_from_schema=True,
    ),
    "parameter-behaviors-dropped-in-schema": StrictModeRule(
        rule_id="parameter-behaviors-dropped-in-schema",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A Parameter attached converters, validators, or traits that "
            "are not captured in the worker schema. Orchestrator-side UI "
            "behavior and worker-side execution diverge."
        ),
        remediation_template=(
            "Parameter '{parameter_name}' carries {dropped_attributes} that "
            "are not serialized into the worker schema. These will not "
            "execute on the orchestrator stub; behavior may differ from "
            "a local-library node."
        ),
        # Reported during library load on the worker; escalating to ERROR
        # would cause the class to be skipped entirely, which is too harsh
        # for an ergonomics warning.
        worker_escalation=False,
    ),
    "parameter-mutation-during-aprocess": StrictModeRule(
        rule_id="parameter-mutation-during-aprocess",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node called add_parameter or remove_parameter during "
            "aprocess. On the worker these changes are local to the "
            "transient node and do not sync back to the orchestrator."
        ),
        remediation_template=(
            "Node '{node_name}' (type '{node_class}') mutated parameter "
            "'{parameter_name}' during aprocess via {mutation}. Emit "
            "AddParameterToNodeRequest or RemoveParameterFromNodeRequest "
            "to propagate the change to the orchestrator."
        ),
    ),
    "worker-reach-into-orchestrator": StrictModeRule(
        rule_id="worker-reach-into-orchestrator",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node running on a worker issued a request whose "
            "authoritative state lives on the orchestrator (flow "
            "graph, connections, parameter registry, config, or "
            "secrets). The request was forwarded to the orchestrator "
            "over the WebSocket bus; each call is a network round-"
            "trip and the returned view is stale-by-call. Events are "
            "the sanctioned cross-side boundary, but authors are "
            "often unaware they are paying for it. Detection runs at "
            "the RemoteHandler dispatch site, which covers both input "
            "hydration (before/after_value_set) and aprocess. "
            "Violations issued from library-internal helper threads "
            "(e.g. ThreadPoolExecutor inside diffusers/transformers) "
            "are not reported because the strict-mode reporter is "
            "task-local; the forward still proceeds normally."
        ),
        remediation_template=(
            "Worker-side node issued '{request_type}' during node "
            "execution. If this is an intentional write (e.g. "
            "publishing a parameter value), ignore. If this is a read "
            "of flow / connection state, consider whether the data "
            "could be passed in via parameters instead of fetched "
            "per-call."
        ),
        worker_escalation=False,
    ),
}
