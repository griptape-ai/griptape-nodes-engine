"""Node carrying everything a schema stub drops, for a library routed to a worker.

The point of this fixture is the combination: the library declares execution dependencies, so
its execution goes to a worker -- and yet the ORCHESTRATOR holds this real class, so the
parameter behaviors that only exist in Python survive. A stub rebuilt from
``WorkerParameterSchema`` carries scalar fields and ``ui_options`` only, so all three of these
would be gone:

- a ``Button`` trait, whose click handler lives on the trait rather than on the node. This is
  the worst of the family, because ``ui_options`` DO serialize, so the button renders and
  looks clickable while being guaranteed to fail (griptape-nodes-engine#5420).
- a converter, which rewrites an incoming value.
- a validator, which refuses one.
- a value hook and a connection hook override, which a stub class does not carry at all.

Module scope stays clean of the execution dependency on purpose: that is the contract that
lets the orchestrator import this at all.
"""

from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload

CLICK_ACKNOWLEDGEMENT = "the node's own handler ran on the orchestrator"


def _shout(value: Any) -> Any:
    if isinstance(value, str):
        return value.upper()
    return value


def _reject_forbidden(parameter: Parameter, value: Any) -> None:  # noqa: ARG001
    if value == "FORBIDDEN":
        msg = "forbidden value"
        raise ValueError(msg)


class BehaviorPreservationNode(DataNode):
    BUTTON_PARAMETER = "model_manager"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        button = Button(full_width=True)
        button.on_click_callback = self._on_button_clicked
        self.add_parameter(
            Parameter(
                name=self.BUTTON_PARAMETER,
                type="str",
                default_value="",
                tooltip="Opens the model manager",
                allowed_modes={ParameterMode.PROPERTY},
                traits={button},
            )
        )
        self.add_parameter(
            Parameter(
                name="mode",
                type="str",
                default_value="plain",
                tooltip="Converted to upper case; refuses FORBIDDEN",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                converters=[_shout],
                validators=[_reject_forbidden],
            )
        )
        self.add_parameter(
            Parameter(
                name="observed_mode",
                type="str",
                default_value="",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def _on_button_clicked(self, _button: Button, _details: ButtonDetailsMessagePayload) -> NodeMessageResult:
        return NodeMessageResult(success=True, details=CLICK_ACKNOWLEDGEMENT)

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Overridden so the value-hook detector has something to find."""
        super().after_value_set(parameter, value)

    def after_incoming_connection(
        self,
        source_node: Any,
        source_parameter: Parameter,
        target_parameter: Parameter,
    ) -> None:
        """Overridden so the connection-hook detector has something to find."""
        super().after_incoming_connection(source_node, source_parameter, target_parameter)

    def process(self) -> None:
        # Deferred on purpose: this library declares it as execution-only, so it is absent
        # from the orchestrator by design.
        import fakeexec  # type: ignore[reportMissingImports]  # noqa: F401

        self.parameter_output_values["observed_mode"] = self.get_parameter_value("mode")
