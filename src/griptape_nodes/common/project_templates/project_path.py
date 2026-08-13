"""Per-platform project path mapping shared by `projects_to_register` and `parent_project_path`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from griptape_nodes.common.project_templates.directory import PerPlatformPathBase
from griptape_nodes.files.path_utils import (
    canonicalize_expanded_for_identity,
    expand_path_fully,
    expansion_introduced_quoting,
    sanitize_path_string,
    unexpanded_references,
)

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
    - `path` is set, both lists are empty, and neither flag is set: the field resolved.
    - `path` is None and at least one list is non-empty: the field declares a variable nothing
      supplied a value for, so there IS no answer. Callers must report this rather than substitute a
      guess -- a wrong path labelled "declared" is worse for users than an honest "unknown".
    - `path` is None and `needs_anchor` is set: the value is relative and there was nowhere to put it.
    - `path` is None and `reference_cycle` is set: the value's references expand into each other, so
      no amount of expansion finishes. The lists are empty because no single variable is at fault --
      every variable involved IS set, and naming one of them as missing would be a lie.
    - `path` is None and `quoted_expansion` is set: a variable's value carried quotes that expansion
      moved into the middle of the path. The lists are empty for the same reason as a cycle -- every
      variable IS set, the value they produced just is not usable as a path.

    Attributes:
        path: Canonical absolute path, or None when the field could not be resolved.
        unresolved_variables: Names from `${NAME}` / `%NAME%` references that expanded to nothing.
        macro_tokens: Names from `{NAME}` macro tokens, which these fields do not support.
        needs_anchor: True when the value is relative but no `anchor_dir` was available to
            resolve it against. Distinguishes "cannot be placed" from "declares a bad variable".
        reference_cycle: True when expansion never reached a fixed point, i.e. the variables refer
            to each other in a cycle.
        quoted_expansion: True when expansion introduced quote characters the author did not write,
            per `expansion_introduced_quoting`.
    """

    path: Path | None
    unresolved_variables: list[str]
    macro_tokens: list[str]
    needs_anchor: bool = False
    reference_cycle: bool = False
    quoted_expansion: bool = False


def resolve_project_path_field(selected: str, anchor_dir: Path | None) -> ResolvedProjectPath:
    """Resolve a declared project path field (`workspace_dir`, `libraries_dir`, `parent_project_path`).

    The supported contract for these fields, in order of application:

    1. `~` and PROCESS/SHELL environment variables are expanded, repeatedly, until the value stops
       changing -- a variable's value may itself contain a reference (`LIBS=${ROOT}/libs`), which a
       single pass would leave half-expanded. The accepted forms are `os.path.expandvars`'s: `$NAME`
       and `${NAME}` everywhere, plus `%NAME%` on Windows. A bare `$NAME` IS expanded like the rest;
       what makes it different is only that an UNEXPANDED one is not reported as missing (see
       `unexpanded_references`), because `$Recycle.Bin` cannot be told apart from a real directory.
       The environment is the one the engine was launched with -- deliberately NOT the project's own
       `environment:` block, which activation applies only AFTER these fields resolve
       (`ProjectManager._activate_project` restores the outgoing project's env, resolves workspace
       and libraries, and only then calls `_apply_project_env`). Honoring a project's own
       `environment:` here would make a field's meaning depend on which project happens to be open.
       That guarantee covers the project being ACTIVATED, not every caller: a project validated or
       registered while some OTHER project is active is read against that project's applied
       `environment:`, since `_apply_project_env` mutates `os.environ` process-wide until the
       active project is swapped out. A value that depends on a variable only that other project
       sets can therefore pass validation and later fail to resolve -- the window is spelled out on
       `ProjectManager._resolve_template_path_field`.
    2. A path still relative AFTER expansion is anchored to `anchor_dir` (the directory of the YAML
       that DECLARED it), so a relative value travels with the project across machines.
    3. The result is canonicalized for identity so two spellings of one directory compare equal.

    Expansion must precede the relative/absolute decision: `Path("${LIBS}/x").is_absolute()` is
    False, so testing absoluteness first would anchor an absolute env-var value under `anchor_dir`
    and -- when the variable is unset -- bake a literal `${LIBS}` directory into the result.

    The string this validates is the string it returns. Nothing below re-expands (hence
    `canonicalize_expanded_for_identity` rather than `canonicalize_for_identity`), because a second
    expansion would make the reported reason describe a value the caller never receives: a field
    whose expansion yields another reference would be refused as declaring an unset variable while
    the path built from it resolved perfectly well.

    Quoting that arrives WITH a variable's value is refused rather than cleaned, because by then it
    cannot be told from a directory name; `expansion_introduced_quoting` draws that line. Quoting the
    author wrote is still cleaned, which is why the declared text is sanitized before expansion and
    the result sanitized again after.

    `{NAME}` macro tokens are NOT supported here (the macro system resolves `directories:`
    `path_macro` fields and node parameters, never these). The dependency runs the wrong way for it:
    building the macro resolution bag needs `workspace_dir` and the project's directories, which are
    among the values these fields produce. They are reported rather than silently becoming a directory
    named `{NAME}`. What counts as an unresolved reference at all is decided by
    `unexpanded_references`, which also explains which forms go unreported when they do not expand.

    Args:
        selected: The declared value, already reduced to one string via `select_project_path`.
        anchor_dir: Directory of the project YAML that declared the field. None when the caller has
            no anchor available, in which case a value that is still relative after expansion
            cannot be placed and comes back as `needs_anchor`.

    Returns:
        A `ResolvedProjectPath`; check `path is None` before using it.
    """
    sanitized = sanitize_path_string(selected)
    fully_expanded = expand_path_fully(sanitized)

    if not fully_expanded.stabilized:
        logger.debug("Project path field %r never stopped expanding; its references cycle", selected)
        return ResolvedProjectPath(
            path=None,
            unresolved_variables=[],
            macro_tokens=[],
            reference_cycle=True,
        )

    # Sanitized a second time because an expanded variable can carry its own quotes or a stray
    # newline, and this is the last point they can be cleaned. It happens HERE rather than inside the
    # canonicalize call so the value being validated below is the value that gets returned.
    # Kept as a string as well: the quoting check below compares character-for-character against the
    # declared text, and a `Path` round-trip rewrites separators before that comparison can be made.
    sanitized_expanded = sanitize_path_string(fully_expanded.path)
    expanded = Path(sanitized_expanded)

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

    # A variable that supplied only part of the path can leave its own quotes stranded in the
    # interior, where the sanitize above cannot see them. Refuse rather than guess: a leading quote
    # makes the value look relative, which would anchor an absolute path under `anchor_dir` and
    # report success while installing somewhere the project never named.
    if expansion_introduced_quoting(sanitized, sanitized_expanded):
        logger.debug(
            "Project path field %r expanded to %r, which quoting made unusable as a path",
            selected,
            sanitized_expanded,
        )
        return ResolvedProjectPath(
            path=None,
            unresolved_variables=[],
            macro_tokens=[],
            quoted_expansion=True,
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
        path=canonicalize_expanded_for_identity(expanded, base=anchor_dir),
        unresolved_variables=[],
        macro_tokens=[],
    )
