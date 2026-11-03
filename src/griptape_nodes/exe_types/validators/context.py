"""What a validator's `check` is handed besides the value: the input it is checking, and its node.

The runner builds one `CheckContext` per value. A check reads what it needs from it: the start of
its message (`subject`), the limits page to link (`docs_url`), a list item's `position`, what the
value's media file holds (`media`), and another parameter's value (`setting`). Nothing is read until
a check asks for it, and what one check reads, the rest of the value's checks reuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.utils.media_probe import MediaFile

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.utils.media_values import MediaKind

__all__ = ["CheckContext", "SettingUnavailableError", "UnknownSettingError"]


class UnknownSettingError(ValueError):
    """A rule reads a parameter the node doesn't have, such as a misspelled name in an `IfParameter`.

    The runner fails the node's checks with it, rather than reading the missing value as None and
    quietly applying the wrong limits.
    """


class SettingUnavailableError(Exception):
    """Another parameter's value can't be read here: it's held in another process.

    The runner skips the input's checks that depend on it, as it skips an input held elsewhere.
    """


@dataclass
class CheckContext:
    """The input a validator is checking, and how to read anything else the check needs.

    Args:
        node: The node about to run.
        parameter: The parameter whose value is being checked.
        value: The value being checked: a single input, one item of a list, or a whole list.
        subject: The start of a message naming the input, such as "Image 2 in 'references'". For a
            validator that checks the whole list, the parameter's name as the UI shows it.
        docs_url: The node's page documenting its input limits, from `BaseNode.input_limits_url`.
        position: An item's 1-based position in a list parameter, or None for a single input or
            the whole list.
    """

    node: BaseNode
    parameter: Parameter
    value: Any
    subject: str
    docs_url: str | None
    position: int | None = None
    _media: MediaFile | None = field(default=None, init=False, repr=False)

    @property
    def media(self) -> MediaFile:
        """What the value's media file holds, read the first time a check asks for each property.

        Each property is None when it can't be read here: the value isn't an image, video or audio
        input, or it's a remote URL, or a file nothing here can open.
        """
        if self._media is None:
            self._media = MediaFile.of(self.value, kind=media_kind_of(self.parameter))
        return self._media

    def setting(self, name: str) -> Any:
        """Another parameter's current value on this node, such as the selected model. See `read_setting`."""
        return read_setting(self.node, name, for_parameter=self.parameter.name)


def read_setting(node: BaseNode, name: str, *, for_parameter: str) -> Any:
    """A parameter's current value on `node`, read to pick or narrow the limits on another parameter.

    Args:
        node: The node about to run.
        name: The parameter to read.
        for_parameter: The parameter whose limits depend on it, for the error message.

    Raises:
        UnknownSettingError: If the node has no parameter called `name`, so a misspelled name fails
            the node's checks instead of reading as None.
        SettingUnavailableError: If the value is held in another process and can't be read here.
    """
    if node.get_parameter_by_name(name) is None:
        msg = (
            f"Attempted to read parameter '{name}' to pick the limits for '{for_parameter}'. "
            f"Failed because the node has no parameter called '{name}'."
        )
        raise UnknownSettingError(msg)
    try:
        return node.get_parameter_value(name)
    except RuntimeError as e:
        raise SettingUnavailableError(name) from e


# The artifact type names a parameter declares for each kind of media.
_KIND_FOR_TYPE: dict[str, MediaKind] = {
    "imageurlartifact": "image",
    "imageartifact": "image",
    "videourlartifact": "video",
    "videoartifact": "video",
    "audiourlartifact": "audio",
    "audioartifact": "audio",
}


def media_kind_of(parameter: Parameter) -> MediaKind | None:
    """The kind of media a parameter holds, or a list parameter's items hold, from its declared type."""
    type_name = parameter.type.strip().lower()
    if type_name.startswith("list[") and type_name.endswith("]"):
        type_name = type_name.removeprefix("list[").removesuffix("]")
    return _KIND_FOR_TYPE.get(type_name)
