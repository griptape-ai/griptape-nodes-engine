"""Save-path macro variables that describe the media inside the file being saved.

A situation macro can use `{media.width}`, `{media.fps?}` and the other names below to put facts
about the saved image or video into its path. The engine reads the values from the bytes being
written, so no node has to pass them. A name with no value for the file (`media.fps` on an image,
any `media.*` on a `.txt`) behaves like any unset variable: an optional slot drops out and a
required slot fails the save.

The `media.` prefix is reserved for these values. Callers cannot supply variables that use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from griptape_nodes.common.macro_parser import ParsedMacro

MEDIA_VARIABLE_PREFIX = "media."

# Decimal places kept for measured values such as frame rate and duration.
_DECIMAL_PLACES = 3


class MediaInfo(BaseModel):
    """What the engine read from a media file, under the names save-path macros use.

    Each artifact metadata model maps its own field names onto these in `to_media_info`, so one
    template works for images and video. A field is None when it does not apply to the file or
    could not be read.
    """

    model_config = ConfigDict(frozen=True)

    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration: float | None = None
    codec: str | None = None
    channels: int | None = None
    color_space: str | None = None

    def to_macro_variables(self) -> dict[str, str]:
        """Return the fields that have values as `media.<field>` macro variables."""
        return {
            f"{MEDIA_VARIABLE_PREFIX}{name}": _format_value(value)
            for name, value in self.model_dump(exclude_none=True).items()
        }


MEDIA_VARIABLE_NAMES = frozenset(f"{MEDIA_VARIABLE_PREFIX}{name}" for name in MediaInfo.model_fields)


def is_media_variable(name: str) -> bool:
    """Return True when `name` uses the reserved `media.` prefix."""
    return name.startswith(MEDIA_VARIABLE_PREFIX)


def references_media_variables(parsed_macro: ParsedMacro) -> bool:
    """Return True when the template uses any `media.*` variable."""
    return any(is_media_variable(info.name) for info in parsed_macro.get_variables())


def unknown_media_variables(parsed_macro: ParsedMacro) -> list[str]:
    """Return the `media.*` names in the template that the engine never fills in, sorted."""
    return sorted(
        info.name
        for info in parsed_macro.get_variables()
        if is_media_variable(info.name) and info.name not in MEDIA_VARIABLE_NAMES
    )


def reserved_variables_in(variables: Mapping[str, object]) -> list[str]:
    """Return the caller-supplied variable names that use the reserved `media.` prefix, sorted."""
    return sorted(name for name in variables if is_media_variable(name))


def _format_value(value: object) -> str:
    """Format a value for a path. Floats keep up to 3 decimals and never use scientific notation.

    24.0 becomes "24" and 23.976 stays "23.976".
    """
    if not isinstance(value, float):
        return str(value)
    text = f"{value:.{_DECIMAL_PLACES}f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text
