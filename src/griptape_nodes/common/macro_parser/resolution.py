"""Resolution logic for macro templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.common.macro_parser.exceptions import MacroResolutionError, MacroResolutionFailureReason
from griptape_nodes.common.macro_parser.segments import (
    MacroVariables,
    ParsedSegment,
    ParsedStaticValue,
    ParsedVariable,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager


@dataclass
class PartiallyResolvedMacro:
    """Result of partially resolving a macro with known variables.

    Contains both resolved segments (known variables → static text) and
    unresolved segments (unknown variables still as variables).
    """

    original_template: str
    segments: list[ParsedSegment]
    known_variables: MacroVariables

    def is_fully_resolved(self) -> bool:
        """Check if all variables have been resolved."""
        return all(isinstance(seg, ParsedStaticValue) for seg in self.segments)

    def to_string(self) -> str:
        """Convert to string (only valid if fully resolved)."""
        if not self.is_fully_resolved():
            unresolved = self.get_unresolved_variables()
            unresolved_names = {var.info.name for var in unresolved}
            msg = "Cannot convert partially resolved macro to string - unresolved variables remain"
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                missing_variables=unresolved_names,
            )
        # All segments are ParsedStaticValue at this point
        return "".join(seg.text for seg in self.segments if isinstance(seg, ParsedStaticValue))

    def get_unresolved_variables(self) -> list[ParsedVariable]:
        """Get list of unresolved variables."""
        return [seg for seg in self.segments if isinstance(seg, ParsedVariable)]


def partial_resolve(
    template: str,
    segments: list[ParsedSegment],
    variables: MacroVariables,
    secrets_manager: SecretsManager | None = None,
) -> PartiallyResolvedMacro:
    """Partially resolve the macro template with known variables.

    Resolves known variables (including env vars and format specs) into static
    text, leaving unknown variables as-is. This is the core resolution logic
    used by both resolve() and find_matches().

    Args:
        template: Original template string
        segments: Parsed segments from template
        variables: Variable name -> value mapping for known variables
        secrets_manager: SecretsManager instance for resolving env vars.
            None disables secret access: a required variable whose value
            references an env var ("$NAME") fails with SECRETS_UNAVAILABLE;
            an optional one is skipped.

    Returns:
        PartiallyResolvedMacro with resolved and unresolved segments

    Raises:
        MacroResolutionError: If:
            - Required variable is provided but env var resolution fails
            - Required variable references an env var and secrets_manager is None
            - Format specifier cannot be applied to value type
    """
    resolved_segments: list[ParsedSegment] = []

    for segment in segments:
        match segment:
            case ParsedStaticValue():
                resolved_segments.append(segment)
            case ParsedVariable():
                if segment.info.name in variables:
                    # Known variable - resolve it
                    resolved_value = resolve_variable(segment, variables, secrets_manager)
                    if resolved_value is not None:
                        # Variable was resolved, add as static
                        resolved_segments.append(ParsedStaticValue(text=resolved_value))
                    # else: Optional variable provided as None, skip it
                    continue

                if segment.info.is_required:
                    # Required variable not in variables dict - keep as unresolved
                    resolved_segments.append(segment)
                    continue

                # Optional variable not in variables dict - skip it
                continue
            case _:
                msg = f"Unexpected segment type: {type(segment).__name__}"
                raise MacroResolutionError(
                    msg,
                    failure_reason=MacroResolutionFailureReason.UNEXPECTED_SEGMENT_TYPE,
                )

    return PartiallyResolvedMacro(
        original_template=template,
        segments=resolved_segments,
        known_variables=variables,
    )


def resolve_variable(
    variable: ParsedVariable, variables: dict[str, str | int], secrets_manager: SecretsManager | None = None
) -> str | None:
    """Resolve a single variable with format specs and env var resolution.

    Args:
        variable: The parsed variable to resolve
        variables: Variable name -> value mapping
        secrets_manager: SecretsManager instance for resolving env vars.
            None disables secret access: a required variable whose value
            references an env var ("$NAME") fails with SECRETS_UNAVAILABLE;
            an optional one is skipped.

    Returns:
        Resolved string value, or None if optional variable not provided

    Raises:
        MacroResolutionError: If required variable missing, env var not found,
            or env var referenced while secrets_manager is None
    """
    variable_name = variable.info.name

    if variable_name not in variables:
        if variable.info.is_required:
            msg = f"Required variable '{variable_name}' not found in variables dict"
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                variable_name=variable_name,
                missing_variables={variable_name},
            )
        # Optional variable not provided, return None to signal it should be skipped
        return None

    value = variables[variable_name]
    try:
        resolved_value: str | int = resolve_env_var(value, secrets_manager)
    except MacroResolutionError as err:
        if err.failure_reason == MacroResolutionFailureReason.SECRETS_UNAVAILABLE and not variable.info.is_required:
            # An optional variable whose value we're not allowed to see is
            # skipped, the same as an optional variable that wasn't provided.
            return None
        raise

    for format_spec in variable.format_specs:
        resolved_value = format_spec.apply(resolved_value)

    # Return fully resolved value as string
    return str(resolved_value)


def resolve_env_var(value: str | int, secrets_manager: SecretsManager | None = None) -> str | int:
    """Resolve environment variables in a value.

    Args:
        value: Value that may contain env var reference (e.g., "$VAR")
        secrets_manager: SecretsManager instance for resolving env vars.
            None means the caller has no secrets access; env var references
            fail with SECRETS_UNAVAILABLE rather than resolving.

    Returns:
        Resolved value (env var substituted if found)

    Raises:
        MacroResolutionError: If value starts with $ but env var not found,
            or if it references an env var while secrets_manager is None
    """
    if not isinstance(value, str):
        # Integer values don't contain env vars, return as-is
        return value

    if not value.startswith("$"):
        # String doesn't reference an env var, return as-is
        return value

    env_var_name = value[1:]

    if secrets_manager is None:
        msg = f"Value references environment variable '{env_var_name}', but no secrets access is available"
        raise MacroResolutionError(
            msg,
            failure_reason=MacroResolutionFailureReason.SECRETS_UNAVAILABLE,
            variable_name=env_var_name,
        )

    env_value = secrets_manager.get_secret(env_var_name, should_error_on_not_found=False)

    if env_value is None:
        msg = f"Environment variable '{env_var_name}' not found"
        raise MacroResolutionError(
            msg,
            failure_reason=MacroResolutionFailureReason.ENVIRONMENT_VARIABLE_NOT_FOUND,
            variable_name=env_var_name,
        )

    # Return resolved env var value
    return env_value
