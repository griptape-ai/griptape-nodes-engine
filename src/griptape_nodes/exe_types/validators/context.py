"""What a validator's `check` is handed besides the value: the input it is checking, and how to read its media file.

The runner builds one `CheckContext` per value. A check reads what it needs from it: the start of
its message (`subject`), the limits page to link (`docs_url`), a list item's `position`, and what the
value's media file holds (`media`). Nothing is read until a check asks for it, and what one check
reads, the rest of the value's checks reuse. A limit that depends on another parameter's value is
an `IfParameter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.validators.base import holds_list
from griptape_nodes.utils.media_probe import MediaFile

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.utils.media_values import MediaKind

__all__ = ["CheckContext"]


class NoMediaFileError(TypeError):
    """A check or condition read `ctx.media` where there's no one file to read, so it could never fire.

    A mistake in the validator, not the input: the runner fails the node's checks with it, naming the
    parameter, rather than quietly skipping the limit.
    """


@dataclass
class CheckContext:
    """The input a validator is checking, and how to read its media file.

    Args:
        parameter: The parameter whose value is being checked.
        value: The value being checked: a single input, one item of a list, or a whole list.
        subject: The start of a message naming the input, such as "Image 2 in 'references'". For a
            validator that checks the whole list, the parameter's name as the UI shows it.
        docs_url: The node's page documenting its input limits, from `BaseNode.input_limits_url`.
        position: An item's 1-based position in a list parameter, or None for a single input or
            the whole list.
    """

    parameter: Parameter
    value: Any
    subject: str
    docs_url: str | None
    position: int | None = None
    _media: MediaFile | None = field(default=None, init=False, repr=False)

    @property
    def media(self) -> MediaFile:
        """What the value's media file holds, read the first time a check asks for each property.

        Each property is None when it can't be read here: a remote URL, or a file nothing here can open.

        Raises:
            NoMediaFileError: If there's no one file to read, so a check or condition on it could never fire:
                the parameter doesn't hold image, video or audio, or this is a whole-list validator's
                context, which holds the list.
        """
        if self._media is not None:
            return self._media
        kind = media_kind_of(self.parameter)
        if kind is None:
            msg = (
                f"Attempted to read the media file of '{self.parameter.name}'. Failed because the "
                f"parameter's type, {self.parameter.type}, isn't an image, video or audio artifact."
            )
            raise NoMediaFileError(msg)
        if self.position is None and holds_list(self.parameter):
            msg = (
                f"Attempted to read the media file of '{self.parameter.name}'. Failed because this "
                "check is given the whole list, not one file; check each item instead."
            )
            raise NoMediaFileError(msg)
        self._media = MediaFile.of(self.value, kind=kind)
        return self._media


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
