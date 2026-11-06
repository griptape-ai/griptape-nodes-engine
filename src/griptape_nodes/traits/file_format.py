from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from griptape_nodes.exe_types.core_types import Trait


class FileFormatKind(StrEnum):
    """The kind of file a format parameter's choices name."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    THREE_D = "three_d"


@dataclass(eq=False)
class FileFormat(Trait):
    """Mark a parameter's value as a file format, such as "png" or "glb".

    A host that runs the workflow reads the kind from the workflow shape, so it can offer only the
    formats it can open. The choices themselves stay on the parameter's `Options` trait.
    """

    kind: FileFormatKind = FileFormatKind.IMAGE
    element_id: str = field(default_factory=lambda: "FileFormat")

    def __init__(self, *, kind: FileFormatKind | str) -> None:
        super().__init__(element_id="FileFormat")
        self.kind = FileFormatKind(kind)

    def to_state(self) -> dict[str, Any]:
        return {"kind": self.kind.value}

    def apply_state(self, state: dict[str, Any]) -> None:
        if "kind" in state:
            self.kind = FileFormatKind(state["kind"])
