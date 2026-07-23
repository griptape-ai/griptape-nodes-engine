"""Fixture nodes for the workflow-bloat regression tests (issue #5177).

These nodes deliberately emit *blob-backed artifacts* (`ImageArtifact`,
`AudioArtifact`) whose `.value` holds raw bytes -- the way a user-authored media
node might, instead of saving to storage and emitting a small URL artifact. When
such a value is emitted to the editor (via `GetAllNodeInfo` results and the
`NodeResolvedEvent`/`ParameterValueUpdateEvent` execution events), it is
base64-encoded inline, producing a multi-megabyte websocket message that the cloud
transport drops -- leaving the canvas empty. The engine's blob-size gate should
blank such values before they are emitted.

Each node's payload size is configurable via a `payload_size` property (settable
with `SetParameterValueRequest`) so a test can bracket the configured threshold. The
payload is prefixed with the node name so two instances produce distinct values.
"""

from __future__ import annotations

from typing import Any

from griptape.artifacts import AudioArtifact, ImageArtifact

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode

# Default raw payload (~1.5 MB), matching the oversized artifacts in the real ~9 MB
# support-case workflow. Tests override this per node via `payload_size`.
DEFAULT_PAYLOAD_SIZE_BYTES = 1_500_000


def _payload(node_name: str, size_bytes: int) -> bytes:
    """Build a distinct byte payload of ``size_bytes``, prefixed with the node name."""
    prefix = node_name.encode()
    filler = max(0, size_bytes - len(prefix))
    return prefix + b"\x00" * filler


class InlineBytesImageNode(DataNode):
    """Emits an ImageArtifact whose ``.value`` holds raw bytes of a configurable size."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="payload_size",
                type="int",
                default_value=DEFAULT_PAYLOAD_SIZE_BYTES,
                tooltip="Raw byte size of the emitted image payload",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_image",
                type="ImageArtifact",
                default_value=None,
                tooltip="Image with an inline byte payload",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        size_bytes = self.get_parameter_value("payload_size")
        self.parameter_output_values["output_image"] = ImageArtifact(
            value=_payload(self.name, size_bytes),
            format="png",
            width=64,
            height=64,
        )


class InlineBytesAudioNode(DataNode):
    """Emits an AudioArtifact with an inline byte payload -- proves the gate is type-agnostic."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="payload_size",
                type="int",
                default_value=DEFAULT_PAYLOAD_SIZE_BYTES,
                tooltip="Raw byte size of the emitted audio payload",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_audio",
                type="AudioArtifact",
                default_value=None,
                tooltip="Audio with an inline byte payload",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        size_bytes = self.get_parameter_value("payload_size")
        self.parameter_output_values["output_audio"] = AudioArtifact(
            value=_payload(self.name, size_bytes),
            format="wav",
        )
