"""Per-platform project path mapping shared by `projects_to_register` and `parent_project_path`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.common.project_templates.directory import PerPlatformPathBase
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    expand_path,
    sanitize_path_string,
    unexpanded_references,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("griptape_nodes")


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


class ResolvedProjectPath(NamedTuple):
    """Outcome of resolving a declared project path field to an absolute path.

    Exactly one of these states holds:
    - `path` is set and both lists are empty: the field resolved.
    - `path` is None and at least one list is non-empty: the field declares a variable nothing
      supplied a value for, so there IS no answer. Callers must report this rather than substitute a
      guess -- a wrong path labelled "declared" is worse for users than an honest "unknown".

    Attributes:
        path: Canonical absolute path, or None when unresolved references remain.
        unresolved_variables: Names from `${NAME}` / `%NAME%` references that expanded to nothing.
        macro_tokens: Names from `{NAME}` macro tokens, which these fields do not support.
        needs_anchor: True when the value is relative but no `anchor_dir` was available to
            resolve it against. Distinguishes "cannot be placed" from "declares a bad variable".
    """

    path: Path | None
    unresolved_variables: list[str]
    macro_tokens: list[str]
    needs_anchor: bool = False


def resolve_project_path_field(selected: str, anchor_dir: Path | None) -> ResolvedProjectPath:
    """Resolve a declared project path field (`workspace_dir`, `libraries_dir`, `parent_project_path`).

    The supported contract for these fields, in order of application:

    1. `~` and PROCESS/SHELL environment variables (`${NAME}`, `%NAME%` on Windows) are expanded.
       The environment is the one the engine was launched with -- deliberately NOT the project's own
       `environment:` block, which activation applies only AFTER these fields resolve
       (`ProjectManager._activate_project` restores the outgoing project's env, resolves workspace
       and libraries, and only then calls `_apply_project_env`). Honoring a project's own
       `environment:` here would make a field's meaning depend on which project happens to be open.
    2. A path still relative AFTER expansion is anchored to `anchor_dir` (the directory of the YAML
       that DECLARED it), so a relative value travels with the project across machines.
    3. The result is canonicalized for identity so two spellings of one directory compare equal.

    Expansion must precede the relative/absolute decision: `Path("${LIBS}/x").is_absolute()` is
    False, so testing absoluteness first would anchor an absolute env-var value under `anchor_dir`
    and -- when the variable is unset -- bake a literal `${LIBS}` directory into the result.

    `{NAME}` macro tokens are NOT supported here (the macro system resolves `directories:`
    `path_macro` fields and node parameters, never these). They are reported rather than silently
    becoming a directory named `{NAME}`. What counts as an unresolved reference at all is decided by
    `unexpanded_references`, which also explains why a bare `$NAME` is left as a literal.

    Args:
        selected: The declared value, already reduced to one string via `select_project_path`.
        anchor_dir: Directory of the project YAML that declared the field. None when the caller has
            no anchor available, in which case a value that is still relative after expansion
            cannot be placed and comes back as `needs_anchor`.

    Returns:
        A `ResolvedProjectPath`; check `path is None` before using it.
    """
    sanitized = sanitize_path_string(selected)
    expanded = expand_path(sanitized)

    # Anything still delimited after expansion means the value has no answer yet. Macro tokens count
    # because they are not part of this contract: reporting one lets the caller say so instead of
    # creating a directory literally named `{outputs}`.
    leftover = unexpanded_references(expanded)
    if leftover.variables or leftover.macro_tokens:
        logger.debug(
            "Project path field %r could not be resolved: unresolved variables %s, macro tokens %s",
            selected,
            leftover.variables,
            leftover.macro_tokens,
        )
        return ResolvedProjectPath(
            path=None,
            unresolved_variables=leftover.variables,
            macro_tokens=leftover.macro_tokens,
        )

    if anchor_dir is None and not expanded.is_absolute():
        logger.debug("Project path field %r is relative and no anchor directory was available", selected)
        return ResolvedProjectPath(
            path=None,
            unresolved_variables=[],
            macro_tokens=[],
            needs_anchor=True,
        )

    return ResolvedProjectPath(
        path=canonicalize_for_identity(expanded, base=anchor_dir),
        unresolved_variables=[],
        macro_tokens=[],
    )
