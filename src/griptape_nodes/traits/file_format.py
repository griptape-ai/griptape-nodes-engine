from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.exe_types.trait_state import state_from_rendered_keys

FILE_FORMAT_UI_OPTION = "file_format"


class FileFormatKind(StrEnum):
    """The kind of file a format parameter's choices name. Values match the editor's file-type names."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    THREE_D = "threed"


@dataclass(eq=False)
class FileFormat(Trait):
    """Mark a parameter's value as a file format, such as "png" or "glb".

    The kind is rendered as the `file_format` UI option, so the editor sees it, a Start Flow parameter
    built from this one carries it, and the workflow shape reports it to a host, which can then offer
    only the formats it can open. The choices themselves stay on the parameter's `Options` trait.
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

    def ui_options_for_trait(self) -> dict:
        return {FILE_FORMAT_UI_OPTION: self.kind.value}

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        return state_from_rendered_keys(ui_options, {FILE_FORMAT_UI_OPTION: "kind"})
