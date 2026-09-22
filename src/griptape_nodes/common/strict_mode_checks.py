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
    "connection-hooks-inert-on-worker": StrictModeRule(
        rule_id="connection-hooks-inert-on-worker",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node class in a worker-hosted library overrides "
            "connection lifecycle hooks (allow/before/after "
            "incoming/outgoing connection and their _removed "
            "variants). Connections are orchestrator-owned state and "
            "these hooks are invoked there, where a worker-hosted "
            "library is represented by a synthesized stub that does "
            "not carry the override -- the author's code never runs, "
            "silently."
        ),
        remediation_template=(
            "Node class '{node_class}' overrides {hook_names}, which "
            "never fire for a worker-hosted (Isolated) library: "
            "connection hooks run on the orchestrator against a stub "
            "class. Dynamic parameters driven by connections are not "
            "supported under isolation; run the library in Shared "
            "mode or remove the override."
        ),
        # Reported during library load on the worker; escalating to ERROR
        # would alarm on a node that otherwise executes fine.
        worker_escalation=False,
    ),
    "value-hooks-execute-only-on-worker": StrictModeRule(
        rule_id="value-hooks-execute-only-on-worker",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description=(
            "A node class in a worker-hosted library overrides "
            "before_value_set/after_value_set. Editor value edits run "
            "these hooks on the orchestrator's stub (never the "
            "author's code); the author's hooks fire on the worker "
            "only during execute-time input hydration, on a transient "
            "node discarded after process."
        ),
        remediation_template=(
            "Node class '{node_class}' overrides {hook_names}. For a "
            "worker-hosted (Isolated) library these fire only during "
            "execute-time input hydration -- transforming values "
            "still works, but the hooks do not run when a value "
            "changes in the editor, and any parameter-list mutation "
            "they make is discarded with the transient node. If the "
            "node needs editor-time reactivity, run the library in "
            "Shared mode."
        ),
        # Same load-time reporting channel as
        # parameter-behaviors-dropped-in-schema; ERROR would be too harsh
        # for a node that still executes correctly.
        worker_escalation=False,
    ),
}
