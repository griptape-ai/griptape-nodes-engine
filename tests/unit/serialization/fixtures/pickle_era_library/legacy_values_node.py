"""Node and value types for the pickle-era format fixtures.

``FixtureMode`` and ``FixtureUrlArtifact`` live here, not in the test module, so the fixtures
cover values whose class comes from a library's own dynamically loaded module.
"""

from __future__ import annotations

from enum import StrEnum

from griptape.artifacts import UrlArtifact

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode

PARAMETER_NAMES = (
    "text",
    "count",
    "flag",
    "ratio",
    "items",
    "mapping",
    "int_keyed",
    "pair",
    "blob",
    "image",
    "images",
    "ruleset",
    "mode",
    "custom_artifact",
    "result",
)


class FixtureMode(StrEnum):
    FAST = "fast"
    SLOW = "slow"


class FixtureUrlArtifact(UrlArtifact):
    pass


class LegacyValuesNode(DataNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        for parameter_name in PARAMETER_NAMES:
            self.add_parameter(
                Parameter(
                    name=parameter_name,
                    type="any",
                    default_value=None,
                    tooltip="",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                )
            )

    def process(self) -> None:
        pass
