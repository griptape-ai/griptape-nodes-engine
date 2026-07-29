"""Core ParsedMacro class - main API for macro templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.common.macro_parser.exceptions import (
    MacroParseFailureReason,
    MacroResolutionError,
    MacroResolutionFailureReason,
    MacroSyntaxError,
)
from griptape_nodes.common.macro_parser.matching import extract_unknown_variables
from griptape_nodes.common.macro_parser.parsing import parse_segments
from griptape_nodes.common.macro_parser.resolution import partial_resolve, resolve_variable
from griptape_nodes.common.macro_parser.segments import (
    MacroVariables,
    ParsedSegment,
    ParsedStaticValue,
    ParsedVariable,
    VariableInfo,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager


# Hard cap on the number of optional-unbound variables in a single template
# during reverse-match. Reverse-matching enumerates 2**k combinations of
# emitted vs. omitted optionals (see #5025 and `find_matches_detailed`); we
# keep k bounded to prevent runaway work on pathological templates. Current
# real-world templates top out at 3 optionals; 5 gives 32 combinations, still
# instant, with headroom for future authoring.
MAX_OPTIONAL_VARIABLES_FOR_REVERSE_MATCH = 5


@dataclass
class ParsedMacro:
    """Parsed macro template with methods for resolving and matching paths.

    This is the main API class for working with macro templates.
    """

    template: str
    segments: list[ParsedSegment] = field(init=False)

    def __post_init__(self) -> None:
        """Parse the macro template string, validating syntax."""
        try:
            segments = parse_segments(self.template)
        except MacroSyntaxError as err:
            msg = f"Attempted to parse template string '{self.template}'. Failed due to: {err}"
            raise MacroSyntaxError(
                msg,
                failure_reason=err.failure_reason,
                error_position=err.error_position,
            ) from err

        if not segments:
            segments.append(ParsedStaticValue(text=""))
        self.segments = segments

    def get_variables(self) -> set[VariableInfo]:
        """Extract all VariableInfo from parsed segments."""
        return {seg.info for seg in self.segments if isinstance(seg, ParsedVariable)}

    def resolve(
        self,
        variables: MacroVariables,
        secrets_manager: SecretsManager | None = None,
    ) -> str:
        """Fully resolve the macro template with variable values.

        With secrets_manager=None, "$NAME" variable values are treated as
        secret references the caller may not see: a required variable fails
        with SECRETS_UNAVAILABLE; an optional one is skipped.
        """
        # Partially resolve with known variables
        partial = partial_resolve(self.template, self.segments, variables, secrets_manager)

        # Check if fully resolved
        if not partial.is_fully_resolved():
            unresolved = partial.get_unresolved_variables()
            unresolved_names = {var.info.name for var in unresolved}
            msg = f"Cannot fully resolve macro - missing required variables: {', '.join(sorted(unresolved_names))}"
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                missing_variables=unresolved_names,
            )

        # Convert to string
        return partial.to_string()

    def matches(
        self,
        path: str,
        known_variables: MacroVariables,
        secrets_manager: SecretsManager | None = None,
    ) -> bool:
        """Check if a path matches this template."""
        result = self.find_matches_detailed(path, known_variables, secrets_manager)
        return result is not None

    def extract_variables(
        self,
        path: str,
        known_variables: MacroVariables,
        secrets_manager: SecretsManager | None = None,
    ) -> MacroVariables | None:
        """Extract variable values from a path (plain string keys)."""
        detailed = self.find_matches_detailed(path, known_variables, secrets_manager)
        if detailed is None:
            return None
        # Convert VariableInfo keys to plain string keys
        return {var_info.name: value for var_info, value in detailed.items()}

    def find_matches_detailed(
        self,
        path: str,
        known_variables: MacroVariables,
        secrets_manager: SecretsManager | None = None,
    ) -> dict[VariableInfo, str | int] | None:
        """Extract variable values from a path with metadata (greedy match).

        This is the advanced version that returns detailed variable metadata with VariableInfo keys.
        Most callers should use extract_variables() for plain dict or matches() for boolean check.

        Given a parsed template and a path, extracts variable values by matching
        the path against the template pattern. Known variables are resolved before
        matching to reduce ambiguity.

        Ambiguity handling for optional variables (#5025). When the template has
        optional-unbound variables, each one INDEPENDENTLY may or may not have
        emitted in ``path``. We enumerate all 2^k combinations of
        emitted/omitted, extract each, and validate via forward round-trip
        (resolve the extracted bag → compare to path). The first combination
        that round-trips wins; combinations are tried in
        popcount-descending order so the "richest" answer (most optionals
        emitted, highest information recovered) is preferred over lossier
        matches. ``k`` is capped by
        ``MAX_OPTIONAL_VARIABLES_FOR_REVERSE_MATCH`` to bound the search.

        MATCHING SCENARIOS (how this method handles different cases):

        Scenario A: All variables known, path matches
            Template: "{inputs}/{file_name}"
            Known: {"inputs": "inputs", "file_name": "photo.jpg"}
            Path: "inputs/photo.jpg"
            Result: {"inputs": "inputs", "file_name": "photo.jpg"}

        Scenario B: All variables known, path doesn't match
            Template: "{inputs}/{file_name}"
            Known: {"inputs": "inputs", "file_name": "photo.jpg"}
            Path: "outputs/photo.jpg"
            Result: None

        Scenario C: Some variables known, path matches
            Template: "{inputs}/{workflow_name}/{file_name}"
            Known: {"inputs": "inputs"}
            Path: "inputs/my_workflow/photo.jpg"
            Result: {"inputs": "inputs", "workflow_name": "my_workflow", "file_name": "photo.jpg"}

        Scenario D: Some variables known, known variable value doesn't match path
            Template: "{inputs}/{workflow_name}/{file_name}"
            Known: {"inputs": "outputs"}
            Path: "inputs/my_workflow/photo.jpg"
            Result: None

        Scenario E: Optional variable present in path (emitted)
            Template: "{inputs}/{workflow_name?:_}{file_name}"
            Known: {"inputs": "inputs"}
            Path: "inputs/my_workflow_photo.jpg"
            Result: {"inputs": "inputs", "workflow_name": "my_workflow", "file_name": "photo.jpg"}

        Scenario F: Optional variable omitted from path
            Template: "{inputs}/{workflow_name?:_}{file_name}"
            Known: {"inputs": "inputs"}
            Path: "inputs/photo.jpg"
            Result: {"inputs": "inputs", "file_name": "photo.jpg"}

        Scenario G: Multi-optional mixed decision (canonical #5025 case)
            Template: "{workspace_dir}/{sub_dirs?:/}{file_name_base}{###?:^_v}.{file_extension}"
            Known: {"workspace_dir": "/ws", "file_extension": "py"}
            Path: "/ws/my_flow_v001.py"
            Result: {"workspace_dir": "/ws", "file_name_base": "my_flow",
                     "_index": 1, "file_extension": "py"}
            Combinations tried (popcount desc):
              (sub_dirs=on, _index=on) → resolves to "/ws/my_flow/_v001.py" — MISS
              (sub_dirs=on, _index=off) → "/ws/my_flow_v001/.py" — MISS
              (sub_dirs=off, _index=on) → "/ws/my_flow_v001.py" — MATCH ✓
            The 4th combination is skipped once the 3rd succeeds.

        Scenario H: Format spec reversal (numeric padding)
            Template: "{inputs}/{frame:03}.png"
            Known: {"inputs": "inputs"}
            Path: "inputs/005.png"
            Result: {"inputs": "inputs", "frame": 5}  # Note: integer value

        Args:
            path: Actual path string to match against template
            known_variables: Dictionary of variables with known values. These will be
                            resolved before matching to reduce ambiguity. Pass empty
                            dict {} if no variables are known.
            secrets_manager: SecretsManager instance for resolving env vars in known
                            variables. None disables secret access ("$NAME" values
                            fail for required variables, skip for optional ones).

        Returns:
            Dictionary mapping VariableInfo to extracted values, or None if path doesn't
            match the template pattern.

        Raises:
            MacroSyntaxError: If the template has more than
                ``MAX_OPTIONAL_VARIABLES_FOR_REVERSE_MATCH`` optional-unbound
                variables (reverse-match search would exceed 2**k combinations).
        """
        # STEP 1: Enumerate optional-unbound variables. These are the free
        # dimensions in the reverse-match search — each independently may or
        # may not have emitted in `path`.
        optional_unbound: list[ParsedVariable] = [
            seg
            for seg in self.segments
            if isinstance(seg, ParsedVariable) and not seg.info.is_required and seg.info.name not in known_variables
        ]
        if len(optional_unbound) > MAX_OPTIONAL_VARIABLES_FOR_REVERSE_MATCH:
            msg = (
                f"Attempted to reverse-match path against template '{self.template}'. "
                f"Failed because the template has {len(optional_unbound)} optional-unbound "
                f"variables — reverse-match caps at "
                f"{MAX_OPTIONAL_VARIABLES_FOR_REVERSE_MATCH} to bound the search space."
            )
            raise MacroSyntaxError(msg, failure_reason=MacroParseFailureReason.TOO_MANY_OPTIONAL_VARIABLES)

        # STEP 2: Zero-optional fast path — the template's ambiguity is only
        # about which optionals emitted. With none, one extraction attempt
        # suffices (no round-trip search needed).
        if not optional_unbound:
            partial = partial_resolve(self.template, self.segments, known_variables, secrets_manager)
            if partial.is_fully_resolved():
                if partial.to_string() == path:
                    return self._merge_known_variables({}, known_variables)
                return None
            extracted = extract_unknown_variables(partial.segments, path)
            if extracted is None:
                return None
            return self._merge_known_variables(extracted, known_variables)

        # STEP 3: Enumerate 2^k combinations of emitted/omitted for the
        # optional-unbound variables, popcount-descending so we prefer the
        # answer that recovers the most information from the path. Bit i in
        # the mask corresponds to `optional_unbound[i]`: 1 = assume emitted
        # (keep the segment during extraction), 0 = assume omitted (drop it).
        num_optionals = len(optional_unbound)
        masks_by_popcount_desc = sorted(range(1 << num_optionals), key=lambda m: (-m.bit_count(), m))

        for mask in masks_by_popcount_desc:
            candidate = self._try_optional_mask(mask, optional_unbound, path, known_variables, secrets_manager)
            if candidate is not None:
                return candidate

        return None

    def _try_optional_mask(
        self,
        mask: int,
        optional_unbound: list[ParsedVariable],
        path: str,
        known_variables: MacroVariables,
        secrets_manager: SecretsManager | None,
    ) -> dict[VariableInfo, str | int] | None:
        """Attempt reverse-match with a specific emitted/omitted combination.

        Bit ``i`` of ``mask`` decides ``optional_unbound[i]``: 1 → keep the
        segment (assume the value is in ``path``), 0 → drop it (assume the
        author omitted the whole slot).

        Returns the extracted variable dict on success (extraction produced
        values AND the forward round-trip resolves back to ``path``
        byte-for-byte), else ``None``.
        """
        # Use identity (id) rather than equality: `ParsedVariable` isn't
        # hashable, but each segment in `self.segments` is a distinct object.
        emitted_ids = {id(optional_unbound[i]) for i in range(len(optional_unbound)) if mask & (1 << i)}
        attempt_segments = self._build_attempt_segments(emitted_ids, known_variables, secrets_manager)

        try:
            extracted = extract_unknown_variables(attempt_segments, path)
        except MacroResolutionError:
            return None
        if extracted is None:
            return None

        # An emitted optional that captured empty is a red flag — it means the
        # position was empty but we're claiming the slot emitted. That's a
        # weaker match than dropping the slot entirely (which will be tried by
        # a lower-popcount mask).
        if any(not var.is_required and (value == "" or value is None) for var, value in extracted.items()):
            return None

        if not self._extraction_round_trips(extracted, path, known_variables, secrets_manager):
            return None

        return self._merge_known_variables(extracted, known_variables)

    def _build_attempt_segments(
        self,
        emitted_ids: set[int],
        known_variables: MacroVariables,
        secrets_manager: SecretsManager | None,
    ) -> list[ParsedSegment]:
        """Materialize the segment list for one emitted/omitted attempt.

        - Static segments pass through.
        - Known variables resolve to static text.
        - Required unbound variables stay as ``ParsedVariable`` for extraction.
        - Optional unbound variables stay if their ``id()`` is in
          ``emitted_ids``, else drop entirely.
        """
        attempt_segments: list[ParsedSegment] = []
        for segment in self.segments:
            if not isinstance(segment, ParsedVariable):
                attempt_segments.append(segment)
                continue
            if segment.info.name in known_variables:
                resolved = resolve_variable(segment, known_variables, secrets_manager)
                if resolved is not None:
                    attempt_segments.append(ParsedStaticValue(text=resolved))
                continue
            if segment.info.is_required or id(segment) in emitted_ids:
                attempt_segments.append(segment)
        return attempt_segments

    def _extraction_round_trips(
        self,
        extracted: dict[VariableInfo, str | int],
        path: str,
        known_variables: MacroVariables,
        secrets_manager: SecretsManager | None,
    ) -> bool:
        """Return True iff resolving the extracted+known bag reproduces ``path``.

        Round-tripping is the truth test for greedy reverse-matches: an
        extraction whose forward render doesn't match ``path`` means the
        greedy anchoring crossed a boundary it shouldn't have.
        """
        full_variables: MacroVariables = dict(known_variables)
        for var, value in extracted.items():
            full_variables[var.name] = value
        try:
            resolved_path = self.resolve(full_variables, secrets_manager)
        except MacroResolutionError:
            return False
        return resolved_path == path

    def _merge_known_variables(
        self,
        extracted: dict[VariableInfo, str | int],
        known_variables: MacroVariables,
    ) -> dict[VariableInfo, str | int]:
        """Overlay ``known_variables`` onto ``extracted`` keyed by VariableInfo."""
        for segment in self.segments:
            if isinstance(segment, ParsedVariable) and segment.info.name in known_variables:
                extracted[segment.info] = known_variables[segment.info.name]
        return extracted
