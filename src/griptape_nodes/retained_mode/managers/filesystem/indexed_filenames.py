"""Picking the next free index for a filename pattern, such as `render_{_index}.png` or `v{###}`."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.common.macro_parser import (
    SEQUENCE_VARIABLE_NAME,
    MacroResolutionError,
    MacroSyntaxError,
    MacroVariables,
    ParsedMacro,
)
from griptape_nodes.common.macro_parser.formats import NumericPaddingFormat, SequenceFormat
from griptape_nodes.common.macro_parser.resolution import partial_resolve
from griptape_nodes.common.macro_parser.segments import ParsedStaticValue, ParsedVariable
from griptape_nodes.files.path_utils import canonicalize_to_posix
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    MacroPath,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.os_events import WriteFileRequest
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

# Placeholder bound to the index slot while GetNextVersionIndexRequest resolves the rest of
# a macro through the project. Picked to be a digit run that never shows up in a real path,
# so its rendered form can be located in the resolved string and swapped back for the slot.
VERSION_INDEX_SENTINEL = 918273645

# Matches one `{...}` variable group in a macro template.
MACRO_VARIABLE_GROUP_PATTERN = re.compile(r"\{[^{}]*\}")


class IndexedFilenames(EngineScoped):
    def bind_project_variables_for_index_scan(
        self, macro_path: MacroPath, workflow: WorkflowRuntime | None
    ) -> MacroPath:
        """Bake project directories and builtins into a macro so only its `{_index}` slot is left open.

        Callers such as `DirectoryDestination` and `build_versioned_sequence_destination` pass
        project macros like `{outputs}/renders_v{###}` without binding `{outputs}`. The index
        scan only sees caller variables, so it would find two unresolved variables and fail.

        The macro is resolved through `GetPathForMacroRequest` with a sentinel number in the
        index slot. The sentinel's rendered text is then swapped back for the original slot
        text, giving an absolute template such as `/project/outputs/renders_v{###}`.

        Args:
            macro_path: MacroPath whose template contains an unresolved `{_index}` slot.
            workflow: The workflow project directories resolve for, or None.

        Returns:
            A MacroPath with only the index slot unresolved. Returns `macro_path` unchanged
            when project resolution fails or the slot cannot be located, so callers that
            bind every variable themselves are scanned as given.
        """
        template = macro_path.parsed_macro.template
        if SEQUENCE_VARIABLE_NAME in macro_path.variables:
            return macro_path

        slot_groups = [
            group
            for group in MACRO_VARIABLE_GROUP_PATTERN.findall(template)
            if self._is_single_variable_group(group, SEQUENCE_VARIABLE_NAME)
        ]
        if not slot_groups:
            return macro_path

        sentinel_vars: MacroVariables = {SEQUENCE_VARIABLE_NAME: VERSION_INDEX_SENTINEL}
        rendered_slots = [ParsedMacro(group).resolve(sentinel_vars) for group in slot_groups]

        result = self.engine.project_manager.get_path_for_macro_in(
            workflow,
            GetPathForMacroRequest(
                parsed_macro=macro_path.parsed_macro,
                variables={**macro_path.variables, **sentinel_vars},
                failure_log_level=logging.DEBUG,
            ),
        )
        if not isinstance(result, GetPathForMacroResultSuccess):
            return macro_path

        resolved = canonicalize_to_posix(result.absolute_path)
        pieces: list[str] = []
        cursor = 0
        for group, rendered in zip(slot_groups, rendered_slots, strict=True):
            position = resolved.find(rendered, cursor)
            if position == -1:
                return macro_path
            pieces.append(resolved[cursor:position])
            pieces.append(group)
            cursor = position + len(rendered)
        pieces.append(resolved[cursor:])

        # A resolved path containing literal braces cannot be re-parsed as a macro.
        try:
            bound_macro = ParsedMacro("".join(pieces))
        except MacroSyntaxError:
            return macro_path

        return MacroPath(parsed_macro=bound_macro, variables={})

    def identify_index_variable(self, parsed_macro: ParsedMacro, variables: MacroVariables) -> ParsedVariable | None:
        """Identify which variable should be used for auto-incrementing.

        Analyzes the macro to find unresolved required variables. Returns None if all
        variables are resolved (fallback to suffix injection), returns ParsedVariable
        if exactly one unresolved variable exists, raises error if multiple unresolved.

        Args:
            parsed_macro: Parsed macro template
            variables: Variable values provided by user

        Returns:
            ParsedVariable if exactly one unresolved variable exists,
            None if all variables resolved (use suffix injection fallback)

        Raises:
            ValueError: If multiple unresolved required variables exist (ambiguous)

        Examples:
            Template: "{outputs}/frame_{frame_num:05}.png"
            Variables: {"outputs": "/path"}
            → Returns ParsedVariable with name="frame_num", format_specs=[NumericPaddingFormat(5)]

            Template: "{outputs}/render.png"
            Variables: {"outputs": "/path"}
            → Returns None (use suffix injection)

            Template: "{outputs}/{batch}/frame_{frame_num}.png"
            Variables: {"outputs": "/path"}
            → Raises ValueError (batch and frame_num both unresolved)
        """
        # Partially resolve to identify unresolved variables
        secrets_manager = self.engine.secrets_manager
        partial = partial_resolve(parsed_macro.template, parsed_macro.segments, variables, secrets_manager)

        # Get unresolved variables (optional variables already filtered out)
        unresolved = partial.get_unresolved_variables()

        if len(unresolved) == 0:
            # All variables resolved - use suffix injection fallback
            return None

        if len(unresolved) > 1:
            # Multiple unresolved - ambiguous which to auto-increment
            unresolved_names = [var.info.name for var in unresolved]
            msg = (
                f"CREATE_NEW policy requires at most one unresolved variable for auto-increment, "
                f"found {len(unresolved)}: {', '.join(unresolved_names)}"
            )
            raise ValueError(msg)

        # Exactly one unresolved variable - return it directly
        return unresolved[0]

    def build_glob_pattern_from_partially_resolved(self, partial_segments: list, index_var_name: str) -> str:
        """Build glob pattern by replacing index variable with wildcards.

        Takes partially resolved segments (from partial_resolve) and replaces the index
        variable with wildcard patterns based on its format specs.

        Args:
            partial_segments: Segments from PartiallyResolvedMacro.segments
            index_var_name: Name of the variable to replace with wildcards

        Returns:
            Glob pattern string with wildcards for index variable

        Examples:
            Segments for "/path/frame_{index:05}.png" with index unresolved:
            → "/path/frame_?????.png"

            Segments for "/path/batch_{index:03}_frame_{index:05}.png":
            → "/path/batch_???_frame_?????.png"

            Segments for "/path/frame_{index}.png" (no padding):
            → "/path/frame_*.png"
        """
        pattern_parts = []

        for segment in partial_segments:
            if isinstance(segment, ParsedStaticValue):
                # Keep static text as-is
                pattern_parts.append(segment.text)
            elif isinstance(segment, ParsedVariable):
                if segment.info.name == index_var_name:
                    # Pick the glob width based on the slot's format spec:
                    #  - NumericPaddingFormat (legacy `{x:NN}`): exact width via
                    #    fixed-count `?` wildcards. Matches the original semantics
                    #    where `:03` means "exactly 3 digits."
                    #  - SequenceFormat (new `###` shorthand): minimum width, so
                    #    overflow values like `_v1000` against `###` are valid
                    #    matches. Use the permissive `*` glob. The `*` may match
                    #    non-numeric siblings (e.g. `foo_vfinal.py`); those are
                    #    skipped downstream when `extract_index_from_filename`
                    #    catches the MacroResolutionError raised by
                    #    `SequenceFormat.reverse("final")`.
                    #  - Neither (no padding info): also `*`; same skip behavior.
                    matched_spec = False
                    for format_spec in segment.format_specs:
                        if isinstance(format_spec, NumericPaddingFormat):
                            pattern_parts.append("?" * format_spec.width)
                            matched_spec = True
                            break
                        if isinstance(format_spec, SequenceFormat):
                            pattern_parts.append("*")
                            matched_spec = True
                            break

                    if not matched_spec:
                        # No padding-style format - match any number of digits
                        pattern_parts.append("*")
                else:
                    # This shouldn't happen - all non-index variables should be resolved
                    msg = f"Unexpected unresolved variable '{segment.info.name}' when building glob pattern"
                    raise ValueError(msg)
            else:
                msg = f"Unexpected segment type '{type(segment).__name__}' when building glob pattern"
                raise TypeError(msg)

        return "".join(pattern_parts)

    def extract_index_from_filename(
        self, filename: str, parsed_macro: ParsedMacro, index_var_name: str, variables: MacroVariables
    ) -> int | None:
        """Extract index value from a filename by reverse-matching against macro.

        Uses the macro's extract_variables() method to parse the filename and extract
        the index variable value.

        Args:
            filename: Filename to parse (e.g., "frame_00123.png")
            parsed_macro: Original parsed macro template
            index_var_name: Name of the index variable to extract
            variables: Known variable values (for partial matching)

        Returns:
            Integer index value if successfully extracted, None if filename doesn't match

        Examples:
            Filename: "frame_00123.png"
            Template: "{outputs}/frame_{frame_num:05}.png"
            Variables: {"outputs": "/path"}
            → Returns 123
        """
        secrets_manager = self.engine.secrets_manager

        # Normalize path separators to POSIX form before reverse-matching. The
        # macro template uses `/` by convention, but the filename comes from
        # `Path.glob()` on the host filesystem (backslashes on Windows), and
        # directory-shaped `variables` values (e.g. `{outputs}` bound to
        # `str(temp_dir)`) may likewise carry `\`. A single-char separator
        # mismatch causes the reverse-matcher to fail static-text alignment
        # and return no matches, which manifested as the sequence-slot scan
        # finding zero existing files on Windows CI.
        # `canonicalize_to_posix` correctly handles UNC, long-path
        # prefix (`\\?\`), long-UNC prefix, drive-letter, and mixed-separator
        # cases — see its docstring for the full contract.
        normalized_filename = canonicalize_to_posix(filename)
        normalized_variables: MacroVariables = {
            key: canonicalize_to_posix(value) if isinstance(value, str) else value for key, value in variables.items()
        }

        # Use macro's extract_variables to reverse-match. Non-numeric siblings caught
        # by the permissive `*` glob (e.g. `workflow_vfinal.py` scanning against
        # `workflow_v{###}.py`) reach `SequenceFormat.reverse()` and raise
        # MacroResolutionError from int("final"). Treat that as "this file doesn't
        # match" — same contract as extract_variables returning None. Anything else
        # matched the glob but isn't a valid sequence entry, so it should be skipped,
        # not crash the scan.
        try:
            extracted = parsed_macro.extract_variables(normalized_filename, normalized_variables, secrets_manager)
        except MacroResolutionError:
            return None

        if extracted is None:
            # Filename doesn't match template
            return None

        if index_var_name not in extracted:
            # Index variable not found in extraction
            return None

        value = extracted[index_var_name]

        # Convert to int (format_spec.reverse() should have done this already)
        if isinstance(value, int):
            return value

        # Try to parse as string
        if isinstance(value, str) and value.isdigit():
            return int(value)

        return None

    def find_next_index_with_gap_fill(self, existing_indices: list[int]) -> int:
        """Find next available index using fill-gaps strategy.

        Args:
            existing_indices: List of existing indices

        Returns:
            Next available index (1-based)

        Examples:
            [] -> 1
            [1, 2, 3] -> 4
            [1, 3, 4] -> 2 (fills gap)
        """
        if not existing_indices:
            return 1

        existing_indices.sort()
        for i in range(1, max(existing_indices) + 1):
            if i not in existing_indices:
                return i

        return max(existing_indices) + 1

    def convert_str_path_to_macro_with_index(self, path_str: str) -> MacroPath:
        """Convert string path to MacroPath with required {_index} variable for indexed filenames.

        This is used when the base filename (without index) is already taken.
        Converts paths like "/outputs/render.png" to template "/outputs/render_{_index}.png".

        Args:
            path_str: String path like "/outputs/render.png"

        Returns:
            MacroPath with required _index variable for indexed filenames

        Examples:
            Input: "/outputs/render.png"
            Output: MacroPath with template "/outputs/render_{_index}.png"
            Behavior: render_1.png → render_2.png → render_3.png → ...

            Input: "/outputs/file"
            Output: MacroPath with template "/outputs/file_{_index}"
            Behavior: file_1 → file_2 → file_3 → ...

        Note:
            The base filename (e.g., "render.png") should be tried first before
            using this template for indexed filenames.
        """
        path = Path(path_str)
        stem = path.stem
        suffix = path.suffix
        parent = str(path.parent)

        if suffix:
            template = f"{parent}/{stem}_{{_index}}{suffix}"
        else:
            template = f"{parent}/{stem}_{{_index}}"

        parsed_macro = ParsedMacro(template)

        return MacroPath(parsed_macro=parsed_macro, variables={})

    def select_collision_walk_macro(
        self, request: WriteFileRequest, file_path: Path
    ) -> tuple[MacroPath, ParsedVariable | None]:
        """Pick the MacroPath the CREATE_NEW collision loop walks forward.

        Returns ``(macro_path, padded_index_var)``. When ``padded_index_var`` is not
        None, the caller walks *its* slot (using ProjectManager so unresolved project
        directories like ``{outputs}`` get substituted each iteration).

        When the caller passed a MacroPath whose macro carries a sequence-slot marker
        (``SequenceFormat`` from ``{###}`` shorthand, or legacy ``NumericPaddingFormat``
        from ``{x:NN}`` / ``{x?:NN}``), AND regardless of whether the caller pre-bound
        that slot, we walk that slot against the user's ORIGINAL macro. Incrementing it
        produces consistent zero-padded width across the sequence
        (``v001 → v002 → v003``). Discrimination goes through ``has_sequence_slot_marker``
        so both marker types are recognized uniformly.

        The bound/unbound distinction matters for *starting* the walk (see the
        start-index logic in ``on_write_file_request``'s CREATE_NEW arm) but not for
        *selecting* the slot: by the time we're in collision-fallback the first
        attempt has already failed via "file exists," and the user's intent for any
        padded slot is "give me a padded index here." Walking unbound padded slots
        keeps optional collisions in their padded form (``_001`` instead of ``_1``);
        walking bound padded slots covers the reverse-match case — a matched file
        whose variables came back from ``ParsedMacro.extract_variables`` lands here
        with the index slot bound, and we still want ``v007 → v008`` instead of
        ``v007_1``.

        Otherwise (plain string path, or a MacroPath without ANY padded slot), fall
        back to ``convert_str_path_to_macro_with_index`` which synthesizes
        ``{stem}_{_index}{ext}`` — original behavior preserved.
        """
        if isinstance(request.file_path, MacroPath):
            for segment in request.file_path.parsed_macro.segments:
                # `has_sequence_slot_marker` recognizes both `SequenceFormat` (from
                # `{###}` shorthand) and legacy `NumericPaddingFormat` (from `{x:NN}`)
                # as sequence-slot markers. We do NOT gate on "not already bound" —
                # the reverse-match flow (#4956) intentionally binds `_index` in the
                # variables dict before landing here, and we still want that slot to
                # be the one we walk forward on collision.
                if isinstance(segment, ParsedVariable) and IndexedFilenames.has_sequence_slot_marker(segment):
                    return request.file_path, segment
        return self.convert_str_path_to_macro_with_index(str(file_path)), None

    def scan_for_next_available_index(
        self,
        parsed_macro: ParsedMacro,
        variables: MacroVariables,
        index_var: ParsedVariable,
    ) -> int | None:
        """Scan existing files and return next available index (preview only - no file creation).

        Uses fill-gaps strategy: if indices 1, 2, 4 exist, returns 3.
        If index variable is optional and base filename is free, returns None.

        This is a preview method - it ONLY scans the filesystem and returns a suggestion.
        It does NOT create any files or acquire any locks.

        Args:
            parsed_macro: Parsed macro template
            variables: Known variable values (index variable NOT included)
            index_var: The parsed variable to use for auto-incrementing

        Returns:
            Next available index (1, 2, 3...), or None if index is optional and base filename is free

        Examples:
            Optional index with base file free:
                Template: "/outputs/render{_index?:_}.png"
                Files: ["/outputs/other.png"]
                Returns: None (use base filename "/outputs/render.png")

            Optional index with base file taken:
                Template: "/outputs/render{_index?:_}.png"
                Files: ["/outputs/render.png"]
                Returns: 1 (use "/outputs/render_1.png")

            Fill gaps strategy:
                Template: "/outputs/render{_index:03}.png"
                Files: ["/outputs/render001.png", "/outputs/render002.png", "/outputs/render004.png"]
                Returns: 3 (fill the gap)

            No existing files:
                Template: "/outputs/render{_index:03}.png"
                Files: []
                Returns: 1 (start with index 1)
        """
        secrets_manager = self.engine.secrets_manager
        index_var_name = index_var.info.name

        # Check if index variable is optional
        is_optional = not index_var.info.is_required

        if is_optional:
            # Try to resolve without the index variable to get base filename
            try:
                base_resolved = parsed_macro.resolve(variables, secrets_manager)
                base_path = Path(base_resolved)
                if not base_path.exists():
                    return None  # Use base filename (no index)
            except MacroResolutionError:
                # Cannot resolve without index - treat as required
                pass

        # Build glob pattern by partially resolving with known variables
        partial = partial_resolve(parsed_macro.template, parsed_macro.segments, variables, secrets_manager)
        glob_pattern = self.build_glob_pattern_from_partially_resolved(partial.segments, index_var_name)

        # Scan existing files matching pattern
        glob_path = Path(glob_pattern)
        if not glob_path.parent.exists():
            # Parent directory doesn't exist - start at index 1
            return 1

        existing_files = list(glob_path.parent.glob(glob_path.name))
        existing_indices = []

        for filepath in existing_files:
            # Pass the full path string. extract_index_from_filename matches against the
            # FULL template (parent-directory segments and all), so the basename never
            # matches and the scan would return 1 every call.
            extracted_index = self.extract_index_from_filename(str(filepath), parsed_macro, index_var_name, variables)
            if extracted_index is not None:
                existing_indices.append(extracted_index)

        return self.find_next_index_with_gap_fill(existing_indices)

    @staticmethod
    def find_padded_unresolved_required(parsed_macro: ParsedMacro, missing_required: set[str]) -> ParsedVariable | None:
        """Find the single missing required variable that opts into auto-index seeding.

        A macro author opts in by writing either ``###`` shorthand (parsed as a
        sequence slot — see :class:`SequenceFormat`) or a single unresolved required
        variable with a ``NumericPaddingFormat`` (``{x:NN}`` — legacy heuristic).
        Either marker is the safety contract: without it, an unresolved ``{shot}``
        could just as plausibly be a variable the user forgot to bind, and silently
        filling it with ``1`` would write data under a name the user never intended.

        Used by the seed step in ``on_write_file_request`` (CREATE_NEW only) — after a
        first-attempt resolve fails with MISSING_REQUIRED_VARIABLES, this picks the slot
        that gets ``1`` stuffed into it for the retry.

        Debugging: a ``None`` return is the most common reason a CREATE_NEW save with
        what looks like a valid auto-index macro instead surfaces ``MISSING_REQUIRED``.
        Walk the gates in order.
        """
        # Gate 1: heuristic only fires when there is exactly ONE missing required var.
        # Two or more → ambiguous which is the index slot; refuse and let the caller
        # surface MISSING_REQUIRED naming every unbound var.
        if len(missing_required) != 1:
            return None
        [name] = missing_required

        # Gate 2: walk the parsed segments to recover the variable's full ParsedVariable
        # (we need its format_specs; the caller only has the name string from the
        # failure). The same name can appear in multiple slots; first occurrence is
        # fine since they all bind to the same value.
        matching: list[ParsedVariable] = []
        for segment in parsed_macro.segments:
            if isinstance(segment, ParsedVariable) and segment.info.name == name:
                matching.append(segment)  # noqa: PERF401  # explicit loop for breakpoint debugging
        if not matching:
            # Shouldn't happen — name came from the parser's own missing set — but
            # guard so a corrupt failure result can't crash.
            return None
        candidate = matching[0]

        # Gate 3: the slot must carry a sequence-allocation marker — either the
        # explicit ``SequenceFormat`` (from ``###`` shorthand) or the legacy
        # ``NumericPaddingFormat`` heuristic. Without it the macro author didn't opt in.
        if not IndexedFilenames.has_sequence_slot_marker(candidate):
            return None

        return candidate

    @staticmethod
    def has_sequence_slot_marker(variable: ParsedVariable) -> bool:
        """Return True when this variable should be treated as a sequence-allocated slot.

        Two paths qualify, ORed together:

        1. **Explicit ``SequenceFormat`` marker** — emitted by ``###`` shorthand in
           the macro template (issue #4902). Unambiguously says "this slot is
           system-allocated; OSManager fills it with a sequence number."
        2. **Legacy ``NumericPaddingFormat`` heuristic** — a ``{x:NN}`` slot that
           the macro author intended to bind, but which CREATE_NEW has historically
           treated as auto-indexable when it's the lone unresolved required variable.
           Kept for backward compatibility with shipping project templates and the
           documented behavior in [macros.md], [situations.md], and the default
           situation macros. The OR is the load-bearing piece of #4902's
           "introduce explicit syntax without breaking existing macros" strategy.

        A follow-up cleanup will retire the heuristic path once project templates
        and docs migrate to ``###``; until then both routes are equivalent.
        """
        return any(isinstance(spec, (SequenceFormat, NumericPaddingFormat)) for spec in variable.format_specs)

    @staticmethod
    def _is_single_variable_group(group: str, variable_name: str) -> bool:
        """Return True if a `{...}` template group parses to exactly one variable named `variable_name`."""
        try:
            parsed = ParsedMacro(group)
        except MacroSyntaxError:
            return False
        variables = parsed.get_variables()
        if len(variables) != 1:
            return False
        return next(iter(variables)).name == variable_name
