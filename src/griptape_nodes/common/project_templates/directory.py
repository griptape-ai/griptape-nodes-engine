"""Directory definition for logical project directories."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from griptape_nodes.common.project_templates.loader import YAMLLineInfo
    from griptape_nodes.common.project_templates.validation import ProjectValidationInfo


class PerPlatformPathBase(BaseModel):
    """Shared base for per-platform string mappings (path macros and project paths).

    At least one of `linux`, `darwin`, `windows`, or `default` must be set.
    `default` is consulted when the active platform's key is absent. Unknown
    keys are rejected so a typo like `osx:` surfaces as a validation error
    instead of silently falling through to `default`.

    Subclasses exist purely to give callers distinct types for two different
    semantic uses (a directory path macro vs. a project YAML path); they share
    every field, validator, and the `select()` body.
    """

    model_config = ConfigDict(extra="forbid")

    linux: str | None = Field(default=None, description="Value used on Linux")
    darwin: str | None = Field(default=None, description="Value used on macOS")
    windows: str | None = Field(default=None, description="Value used on Windows")
    default: str | None = Field(default=None, description="Fallback when the active platform's key is unset")

    @model_validator(mode="after")
    def _at_least_one_key(self) -> Self:
        if self.linux is None and self.darwin is None and self.windows is None and self.default is None:
            msg = f"{type(self).__name__} requires at least one of 'linux', 'darwin', 'windows', or 'default'"
            raise ValueError(msg)
        return self

    def select(self) -> str | None:
        """Return the value for the active platform, falling back to `default`."""
        active = active_platform_key()
        if active == "linux" and self.linux is not None:
            return self.linux
        if active == "darwin" and self.darwin is not None:
            return self.darwin
        if active == "windows" and self.windows is not None:
            return self.windows
        return self.default


class PerPlatformPathMacro(PerPlatformPathBase):
    """Per-platform path macro mapping for directory definitions."""


def active_platform_key() -> str:
    """Map sys.platform to one of the per-platform mapping keys, or `""` if it has none.

    An empty return is not an error -- it means this platform cannot be named in a per-platform
    mapping at all, because `PerPlatformPathBase` forbids keys outside `linux`/`darwin`/`windows`.
    `select()` reads it as "go straight to `default`", and callers phrasing an error should read it as
    "`default` is the only remedy available here".
    """
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return ""


class ActivePlatform(NamedTuple):
    """The active platform as a `select()`-failed message needs to talk about it.

    Both fields come from one `active_platform_key()` call on purpose. A message has to name the
    platform *and* propose a fix, and those two halves are only consistent if they agree on which
    platform is active -- naming `windows` while suggesting the remedy for a keyless platform is
    worse than either mistake alone.

    Attributes:
        key: The mapping key for this platform, or `""` if it has none.
        display: How to name this platform to a user who has to edit the YAML.
    """

    key: str
    display: str


def active_platform() -> ActivePlatform:
    """Describe the active platform for a message a user has to act on.

    `sys.platform` is the wrong string to show them: it says `win32`, while the key they need to add
    to their YAML is `windows`. Report the mapping key instead, so "no path for this platform
    (windows)" names something that can appear in the file.

    On a platform with no key, `sys.platform` would be worse than useless rather than merely
    imprecise: `extra="forbid"` rejects any key that is not `linux`/`darwin`/`windows`, so naming
    `freebsd13` points at a fix that fails validation. Say it is unsupported and keep the raw name
    only as parenthetical detail, which is still what someone would quote in a bug report.
    """
    key = active_platform_key()
    if key:
        return ActivePlatform(key=key, display=key)
    return ActivePlatform(key="", display=f"unsupported ({sys.platform})")


class DirectoryDefinition(BaseModel):
    """Definition of a logical directory in the project."""

    name: str = Field(description="Logical name (e.g., 'inputs', 'outputs')")
    path_macro: str | PerPlatformPathMacro = Field(
        description="Path string (may contain macros/env vars), or a per-platform mapping"
    )
    description: str | None = Field(
        default=None,
        description="Human-readable explanation of what this directory is for and how it's used.",
    )

    @staticmethod
    def merge(
        base: DirectoryDefinition,
        overlay_data: dict[str, Any],
        field_path: str,
        validation_info: ProjectValidationInfo,
        line_info: YAMLLineInfo,
    ) -> DirectoryDefinition:
        """Merge overlay fields onto base directory.

        Field-level merge behavior:
        - path_macro: Use overlay if present, else base. Atomic — when overlay supplies the
          per-platform mapping form, it fully replaces the base value (no per-key deep merge).
        - description: Use overlay if the key is present (explicit null clears the inherited
          value), else inherit base.

        Args:
            base: Complete base directory
            overlay_data: Partial directory dict from overlay
            field_path: Path for validation errors (e.g., "directories.inputs")
            validation_info: Shared validation info
            line_info: Line tracking from overlay

        Returns:
            New merged DirectoryDefinition
        """
        # Start with base fields
        merged_data: dict[str, Any] = {
            "name": base.name,
            "path_macro": base.path_macro,
            "description": base.description,
        }

        # Apply overlay if present
        if "path_macro" in overlay_data:
            merged_data["path_macro"] = overlay_data["path_macro"]

        if "description" in overlay_data:
            merged_data["description"] = overlay_data["description"]

        try:
            return DirectoryDefinition.model_validate(merged_data)
        except ValidationError as e:
            # Convert Pydantic validation errors to our validation_info format
            for error in e.errors():
                error_field_path = ".".join(str(loc) for loc in error["loc"])
                full_field_path = f"{field_path}.{error_field_path}"
                message = error["msg"]
                line_number = line_info.get_line(full_field_path)

                validation_info.add_error(
                    field_path=full_field_path,
                    message=message,
                    line_number=line_number,
                )

            # Return base on validation error (fault-tolerant)
            return base
