"""Derivation rules for macro variables.

A derivation rule computes a macro variable from other variables plus project
state (e.g. the current project template). Rules are a third kind of variable
source, alongside:

- **Builtins** (`workspace_dir`, `outputs`, etc.) -- resolved by the macro
  resolver against the project's directory definitions.
- **Caller-supplied** (`node_name`, `file_name_base`, etc.) -- values only the
  caller knows, passed in via the variables dict.
- **Derived** (this module) -- values computed from other variables + project
  state, applied as a pre-resolution pass before handing a MacroPath to the
  resolver.

A rule fires only when the template references its output variable, all its
required inputs are present, and the output isn't already supplied by the
caller. Returning None from `derive` means "abstain" -- the variable stays
unset so optional macro slots degrade cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.common.macro_parser import ParsedMacro, ParsedVariable
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

logger = logging.getLogger("griptape_nodes")


@dataclass(frozen=True)
class DerivationRule:
    """A rule for deriving a macro variable from other variables + project state.

    Attributes:
        name: Macro variable this rule produces.
        requires: Variable names that must all be present for the rule to fire.
        derive: Computes the derived value. Receives the full variables mapping
            (useful when derivation needs context beyond `requires`). Returns
            None to abstain.
    """

    name: str
    requires: frozenset[str]
    derive: Callable[[Mapping[str, str | int]], str | None]


def apply_derivation_rules(
    macro_path: MacroPath,
    rules: Sequence[DerivationRule],
) -> MacroPath:
    """Run derivation rules against a MacroPath and inject produced variables.

    Rules are applied independently in order. If a rule's output feeds another
    rule's `requires`, list the producing rule first. (No multi-pass fixpoint:
    keeping evaluation single-pass makes ordering explicit and avoids cycles.)

    Returns the original MacroPath unchanged when no rule fires.
    """
    referenced = _template_variable_names(macro_path.parsed_macro)
    variables = dict(macro_path.variables)
    changed = False
    for rule in rules:
        if rule.name not in referenced:
            continue
        if rule.name in variables:
            continue
        if not rule.requires.issubset(variables):
            continue
        derived = rule.derive(variables)
        if derived is None:
            continue
        variables[rule.name] = derived
        changed = True
    if not changed:
        return macro_path
    return MacroPath(macro_path.parsed_macro, variables)


def _template_variable_names(parsed_macro: ParsedMacro) -> set[str]:
    """Return the set of variable names referenced by a parsed template."""
    return {segment.info.name for segment in parsed_macro.segments if isinstance(segment, ParsedVariable)}


def resolve_file_extension_directory(extension: str, extra_vars: Mapping[str, str | int] | None = None) -> str | None:
    """Resolve an extension to a folder fragment via the current project's template.

    Looks up the extension (case-insensitively) in the current project's
    `file_extension_directories` mapping. Plain names like `"images"` are returned
    as-is; values containing macro syntax (e.g. `"{outputs}/videos"`) are
    resolved via `GetPathForMacroRequest` against the project's builtins and
    directory definitions, plus any caller-supplied `extra_vars` (e.g.
    `node_name`, `parameter_name`, `sub_dirs`, `_index`). Filename parts
    (`file_name_base`, `file_extension`) are intentionally excluded --
    extension directories are a routing layer, not a filename layer.

    Returns None when the extension is empty, no project is loaded, the
    extension is unmapped, or resolution fails -- so the optional
    `{file_extension_directory?:/}` slot degrades cleanly instead of routing
    unknown types into an arbitrary folder or surfacing as a crash.
    """
    if not extension:
        return None
    project_result = GriptapeNodes.handle_request(GetCurrentProjectRequest(broadcast_result=False))
    if not isinstance(project_result, GetCurrentProjectResultSuccess):
        return None
    raw_macro = project_result.project_info.template.file_extension_directories.get(extension.lower())
    if raw_macro is None:
        return None
    # Plain folder name -- skip the event round-trip.
    if "{" not in raw_macro:
        return raw_macro
    # Filename parts belong to the situation macro's filename section, not
    # the routing layer. Smuggling them through extension directories would let
    # routing encode filenames.
    resolution_vars: dict[str, str | int] = {
        k: v for k, v in (extra_vars or {}).items() if k not in ("file_name_base", "file_extension")
    }
    resolve_result = GriptapeNodes.handle_request(
        GetPathForMacroRequest(parsed_macro=ParsedMacro(raw_macro), variables=resolution_vars, broadcast_result=False)
    )
    if not isinstance(resolve_result, GetPathForMacroResultSuccess):
        logger.warning(
            "Failed to resolve file_extension_directories value for '%s' (%r): %s. Falling back to no routing.",
            extension,
            raw_macro,
            resolve_result.result_details,
        )
        return None
    # POSIX form so the value composes cleanly with other forward-slash macro
    # segments on Windows (str(Path(...)) would emit backslashes).
    return resolve_result.resolved_path.as_posix()


def _derive_file_extension_directory(variables: Mapping[str, str | int]) -> str | None:
    extension = variables.get("file_extension")
    if not isinstance(extension, str):
        return None
    return resolve_file_extension_directory(extension, variables)


FILE_EXTENSION_DIRECTORY_RULE = DerivationRule(
    name="file_extension_directory",
    requires=frozenset({"file_extension"}),
    derive=_derive_file_extension_directory,
)


# Engine-hardcoded registry. Add new rules here; ordering matters when a rule's
# output feeds another rule's `requires`.
DERIVATION_RULES: tuple[DerivationRule, ...] = (FILE_EXTENSION_DIRECTORY_RULE,)
