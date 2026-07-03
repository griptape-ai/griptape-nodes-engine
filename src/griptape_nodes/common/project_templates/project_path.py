"""Per-platform project path mapping shared by `projects_to_register` and `parent_project_path`."""

from __future__ import annotations

from griptape_nodes.common.project_templates.directory import PerPlatformPathBase


class PerPlatformProjectPath(PerPlatformPathBase):
    """Per-platform mapping for a project YAML path.

    Used by `projects_to_register` (engine config) and `parent_project_path`
    (project template) to express a single logical project that lives at
    different filesystem paths on different operating systems. Shares the
    `linux`/`darwin`/`windows`/`default` shape with `PerPlatformPathMacro`
    via a common base; the distinct type lets the schema express which field
    accepts which.

    At least one of `linux`, `darwin`, `windows`, or `default` must be set.
    `default` is consulted when the active platform's key is absent.
    """


def select_project_path(value: str | PerPlatformProjectPath | None) -> str | None:
    """Reduce a per-platform path union to a single string for the active platform.

    - `None` returns `None` (no path declared).
    - A plain string is passed through unchanged.
    - A `PerPlatformProjectPath` returns its `.select()` value, which may be
      `None` when no key matches the active platform and `default` is unset
      (callers are expected to skip-with-warning in that case).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.select()
