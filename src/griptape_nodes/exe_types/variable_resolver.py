from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, ClassVar

from griptape_nodes.common.macro_parser.core import ParsedMacro
from griptape_nodes.common.macro_parser.exceptions import MacroResolutionError, MacroSyntaxError
from griptape_nodes.common.macro_parser.segments import ParsedVariable

# Sentinel meaning "the flow lookup was attempted but the node is not in any flow."
_NO_FLOW: object = object()

# Caches get_variables_for_macro_resolution() result for the duration of one aprocess() call.
# None = not yet computed; _NO_FLOW = computed but node has no parent flow; dict = resolved vars.
# Reset and optionally pre-seeded by aprocess_scope() via VariableResolver.seed_cache().
_aprocess_variable_cache: ContextVar[dict | object | None] = ContextVar(
    "_variable_resolver_aprocess_cache", default=None
)


class VariableResolver:
    """Resolves inline {VAR} macro references in node parameter values during aprocess().

    All GriptapeNodes singleton access is concentrated in this class's static methods,
    keeping the lazy-import cycle-break in one place rather than scattered across BaseNode.
    """

    _HAS_VARIABLE_MACRO: ClassVar[re.Pattern[str]] = re.compile(r"\{[A-Za-z_]")
    # Matches a single {CONTENT} token with no nested braces (safe to pass to ParsedMacro).
    _MACRO_TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"\{([^{}]*)\}")

    @staticmethod
    def contains_variable_macro(value: Any) -> bool:
        """Return True if value is, or recursively contains, a str with a variable macro reference."""
        if isinstance(value, str):
            return bool(VariableResolver._HAS_VARIABLE_MACRO.search(value))
        if isinstance(value, dict):
            return any(VariableResolver.contains_variable_macro(v) for v in value.values())
        if isinstance(value, list):
            return any(VariableResolver.contains_variable_macro(item) for item in value)
        return False

    @staticmethod
    def resolve_macro_token(token: str, variables: dict[str, str | int]) -> str:
        """Try to resolve a single {VAR} or {VAR:spec} token against the variable dict.

        Returns the resolved string on success, or the original token if the variable
        is unknown, the token is not a macro reference, or parsing fails.

        Variable values are substituted literally — NOT routed through env-var resolution.
        A value like "$HOME" is treated as the string "$HOME", not expanded to the home
        directory. This prevents both secret exfiltration and silent no-ops on dollar-sign
        values (e.g. "$50", "$HOME/x").
        """
        try:
            parsed = ParsedMacro(token)
        except MacroSyntaxError:
            return token
        if not parsed.get_variables():
            return token
        # _MACRO_TOKEN matches exactly one {VAR} or {VAR:spec} per token, so there is at most
        # one ParsedVariable segment. Iterate segments directly (not get_variables()) to retain
        # format_specs, which are stripped by get_variables().
        parsed_var = next((seg for seg in parsed.segments if isinstance(seg, ParsedVariable)), None)
        if parsed_var is None or parsed_var.info.name not in variables:
            return token
        value: str | int = variables[parsed_var.info.name]
        try:
            for format_spec in parsed_var.format_specs:
                value = format_spec.apply(value)
        except MacroResolutionError:
            return token
        return str(value)

    @staticmethod
    def resolve_string(text: str, variables: dict[str, str | int]) -> str:
        """Substitute all {VAR} tokens in text using the provided variable dict."""
        return VariableResolver._MACRO_TOKEN.sub(
            lambda m: VariableResolver.resolve_macro_token(m.group(0), variables),
            text,
        )

    @staticmethod
    def resolve_value(value: Any, variables: dict[str, str | int]) -> Any:
        """Recursively substitute {VAR} references in any str/dict/list value."""
        if isinstance(value, str):
            if VariableResolver._HAS_VARIABLE_MACRO.search(value):
                return VariableResolver.resolve_string(value, variables)
            return value
        if isinstance(value, dict):
            return {k: VariableResolver.resolve_value(v, variables) for k, v in value.items()}
        if isinstance(value, list):
            return [VariableResolver.resolve_value(item, variables) for item in value]
        return value

    @staticmethod
    def seed_cache(variables: dict[str, str | int] | None) -> object:
        """Pre-seed the per-aprocess variable cache. Returns an opaque reset token."""
        return _aprocess_variable_cache.set(variables)

    @staticmethod
    def reset_cache(token: object) -> None:
        """Reset the per-aprocess variable cache to its state before seed_cache was called."""
        _aprocess_variable_cache.reset(token)  # type: ignore[arg-type]

    @staticmethod
    def is_substitution_enabled() -> bool:
        """Return True if variable substitution is enabled for the active workflow."""
        # GriptapeNodes import is lazy to avoid circular dependency between exe_types and retained_mode.
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.WorkflowManager().is_variable_substitution_enabled()

    @staticmethod
    def get_variables_if_enabled(node_name: str) -> dict[str, str | int] | None:
        """Return the variable dict if substitution is enabled, else None.

        Checks the per-aprocess cache first to avoid repeated singleton lookups.
        Returns None if: substitution is disabled, node has no parent flow,
        or the cache indicates the flow lookup already failed (_NO_FLOW).

        NOTE: The cache has no per-flow key — it stores a single dict for the duration
        of the enclosing aprocess_scope(). In practice aprocess_scope() is entered once
        per node execution and the cache is reset on exit, so the "one node per scope"
        invariant holds. The edge case (another node in a *different* flow having
        get_parameter_value() called during this node's aprocess) would return variables
        from the wrong flow, but cross-node reads during aprocess are uncommon enough
        to leave as a known limitation rather than add per-flow keying overhead.
        For worker-executed nodes the cache is pre-seeded by aprocess_scope() from the
        orchestrator-resolved variable dict, so this fallback path is only reached for
        in-process nodes whose request predates the variables field (e.g. unit tests).
        """
        # GriptapeNodes import is lazy to avoid circular dependency between exe_types and retained_mode.
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        if not GriptapeNodes.WorkflowManager().is_variable_substitution_enabled():
            return None

        cached = _aprocess_variable_cache.get()
        if cached is _NO_FLOW:
            return None
        if cached is not None:
            return cached  # type: ignore[return-value]

        from griptape_nodes.retained_mode.events.variable_events import (
            GetVariablesRequest,
            GetVariablesResultSuccess,
        )
        from griptape_nodes.retained_mode.variable_types import VariableScope

        try:
            flow_name = GriptapeNodes.NodeManager().get_node_parent_flow_by_name(node_name)
        except KeyError:
            _aprocess_variable_cache.set(_NO_FLOW)
            return None
        result = GriptapeNodes.handle_request(
            GetVariablesRequest(starting_flow=flow_name, lookup_scope=VariableScope.HIERARCHICAL)
        )
        if not isinstance(result, GetVariablesResultSuccess):
            _aprocess_variable_cache.set(_NO_FLOW)
            return None
        resolved = VariableResolver._filter_for_substitution(result.variables)
        _aprocess_variable_cache.set(resolved)
        return resolved

    @staticmethod
    def references_variable(value: Any, variable_name: str) -> bool:
        """Return True if value contains a macro reference to the given variable name.

        Handles format specs, optional markers, and default values:
        {VAR}, {VAR:lower}, {VAR?}, {VAR|default} all count as referencing VAR.
        Recurses into dicts and lists.
        """
        if isinstance(value, str):
            for match in VariableResolver._MACRO_TOKEN.finditer(value):
                content = match.group(1)
                name = content.split("|")[0].split(":")[0].rstrip("?").strip()
                if name == variable_name:
                    return True
            return False
        if isinstance(value, dict):
            return any(VariableResolver.references_variable(v, variable_name) for v in value.values())
        if isinstance(value, list):
            return any(VariableResolver.references_variable(item, variable_name) for item in value)
        return False

    @staticmethod
    def _filter_for_substitution(variables: dict[str, Any]) -> dict[str, str | int]:
        """Filter a name→value dict to only str/int values (excluding bool) for macro substitution."""
        return {
            name: value
            for name, value in variables.items()
            if isinstance(value, (str, int)) and not isinstance(value, bool)
        }
