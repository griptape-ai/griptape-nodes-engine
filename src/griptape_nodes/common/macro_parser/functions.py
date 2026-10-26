"""Function calls inside inline macro tokens: ``{path_join(outputs, name)}``.

Grammar of a token's content (the text between the braces)::

    token   := call (":" format_spec)*
    call    := NAME "(" [arg ("," arg)*] ")"
    arg     := call | NAME | STRING | NUMBER
    NAME    := [A-Za-z_][A-Za-z0-9_]*
    STRING  := '"' ... '"' | "'" ... "'"     (backslash escapes the quote or a backslash)
    NUMBER  := ["-"] digits ["." digits]

A bare ``NAME`` argument is a variable reference, looked up in the same variable dict the
``{VAR}`` syntax uses. The format specs after the call are the ordinary macro format specs
(``:lower``, ``:03``, ...) applied to the call's result. Braces cannot appear anywhere inside a
token, string literals included, because the token scanner stops at the first ``}``.

Safety: functions come from the fixed table in this module and nothing else. There is no
``eval``, no attribute access, no indexing, and no way to name a Python object. A call that names
a function not in the table leaves the token verbatim, as an unknown ``{VAR}`` does, so prompt text
such as ``{foo()}`` is untouched.

Paths: an absolute path the user supplies (a literal or variable as ``path_join``'s first argument,
e.g. a shared ``/Volumes/...`` folder) is allowed. What is refused is escaping through the call
itself: ``..`` in ``path_join``'s later arguments may not climb above its first argument, and a later
argument may not be an absolute path that replaces the folder before it. ``temp_dir`` and
``temp_file`` must stay inside the workspace or project folder; see ``MacroFunctionContext.confine``.

Forward resolution only: a macro that contains a function call cannot be reverse-matched against
an existing path (``ParsedMacro.extract_variables``), because most functions are not invertible.
Function calls are a feature of inline parameter substitution (``VariableResolver``), not of
project situation templates parsed by ``ParsedMacro``.
"""

# Path functions work on path TEXT with os.path: normpath collapses ".." lexically, which pathlib
# deliberately does not, and the result must stay the string the user built.
# ruff: noqa: PTH117, PTH118, PTH119, PTH120, PTH122

from __future__ import annotations

import math
import os
import re
import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from griptape_nodes.common.macro_parser.exceptions import (
    MacroFunctionArgumentError,
    MacroFunctionArityError,
    MacroFunctionError,
    MacroFunctionSyntaxError,
    MacroPathEscapeError,
    MacroResolutionError,
    MacroResolutionFailureReason,
    MacroSyntaxError,
)
from griptape_nodes.common.macro_parser.parsing import parse_format_spec
from griptape_nodes.files.path_utils import canonicalize_for_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.common.macro_parser.formats import FormatSpec
    from griptape_nodes.common.macro_parser.segments import MacroVariables

MacroValue = str | int | float

# Variable names that locate the folders temp paths are confined to. These are project
# builtins, reserved names a flow variable cannot shadow.
WORKSPACE_DIR_VARIABLE = "workspace_dir"
PROJECT_DIR_VARIABLE = "project_dir"
TEMP_DIR_VARIABLE = "temp"

_DEFAULT_NOW_FORMAT = "%Y-%m-%d_%H-%M-%S"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d"
_DEFAULT_RANDOM_MAX = 100
_DEFAULT_RANDOM_TEXT_LENGTH = 8
_MAX_RANDOM_TEXT_LENGTH = 256
_RANDOM_TEXT_ALPHABET = string.ascii_lowercase + string.digits
_TEMP_FILE_ID_LENGTH = 12

# A token is a call when its content starts with a name directly followed by "(".
_LOOKS_LIKE_CALL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class MacroFunctionInfo:
    """What the editor needs to list one macro function.

    Attributes:
        name: Function name as written in a macro (``path_join``)
        signature: Call shape with argument names; ``?`` marks optional, ``...`` repeats
        description: One line for artists
    """

    name: str
    signature: str
    description: str


@dataclass(frozen=True)
class VariableRef:
    """A bare name argument: a variable lookup."""

    name: str
    position: int


@dataclass(frozen=True)
class Literal:
    """A quoted string or number argument."""

    value: MacroValue
    position: int


@dataclass(frozen=True)
class FunctionCall:
    """A call to a function in the table."""

    name: str
    args: list[FunctionCall | VariableRef | Literal]
    position: int


@dataclass(frozen=True)
class ParsedFunctionToken:
    """A whole token: the outer call plus the format specs applied to its result."""

    call: FunctionCall
    format_specs: list[FormatSpec]

    @property
    def is_volatile(self) -> bool:
        """Whether resolving twice can give different text (time, randomness, fresh temp names)."""
        return _call_is_volatile(self.call)

    def variable_names(self) -> set[str]:
        """Every variable the token reads, at any nesting depth."""
        names: set[str] = set()
        _collect_variable_names(self.call, names)
        return names


@dataclass
class MacroFunctionContext:
    """What functions may read while they run: variables and the folders paths must stay inside."""

    variables: MacroVariables
    allowed_roots: list[Path] = field(default_factory=list)
    workspace_dir: Path | None = None

    @classmethod
    def from_variables(cls, variables: MacroVariables) -> MacroFunctionContext:
        """Build a context whose allowed roots are the workspace and project folders in `variables`.

        Relative paths are anchored to the workspace folder, the same rule the engine applies when
        it resolves a relative path (``resolve_workspace_path``). With neither folder present (no
        project context), temp paths are not checked.
        """
        workspace_dir: Path | None = None
        workspace_value = variables.get(WORKSPACE_DIR_VARIABLE)
        if isinstance(workspace_value, str) and workspace_value:
            workspace_dir = canonicalize_for_identity(workspace_value)

        roots: list[Path] = []
        if workspace_dir is not None:
            roots.append(workspace_dir)
        project_value = variables.get(PROJECT_DIR_VARIABLE)
        if isinstance(project_value, str) and project_value:
            roots.append(canonicalize_for_identity(project_value, base=workspace_dir))
        return cls(variables=variables, allowed_roots=roots, workspace_dir=workspace_dir)

    def confine(self, function_name: str, position: int, path: str) -> str:
        """Return `path` if it stays inside an allowed root, otherwise raise MacroPathEscapeError.

        "Inside" is decided on the canonical identity of the path (``canonicalize_for_identity``:
        ``~`` and environment variables expanded, relative paths anchored to the workspace, ``..``
        collapsed, symlinks followed), so neither ``..`` segments nor a symlink pointing out of
        the workspace gets past it. The returned text is the path as computed, not the canonical
        form, so the user sees the path they built.
        """
        if not self.allowed_roots:
            return path
        identity = canonicalize_for_identity(path, base=self.workspace_dir)
        if any(identity.is_relative_to(root) for root in self.allowed_roots):
            return path
        roots = " or ".join(f"'{root}'" for root in self.allowed_roots)
        msg = (
            f"Attempted to build a path with '{function_name}' at character {position + 1}. "
            f"Failed because '{path}' is outside the workspace and project folders ({roots})."
        )
        raise MacroPathEscapeError(
            msg,
            failure_reason=MacroResolutionFailureReason.PATH_ESCAPE,
            function_name=function_name,
            position=position,
        )


@dataclass(frozen=True)
class _FunctionSpec:
    name: str
    signature: str
    description: str
    min_args: int
    max_args: int | None  # None = any number
    impl: Callable[[_CallSite, list[MacroValue]], MacroValue]
    volatile: bool = False


@dataclass(frozen=True)
class _CallSite:
    """Where a function is being called from, for error messages and context access."""

    name: str
    position: int
    context: MacroFunctionContext
    arg_positions: list[int]

    def arg_error(self, index: int, reason: str) -> MacroFunctionArgumentError:
        position = self.position
        if index < len(self.arg_positions):
            position = self.arg_positions[index]
        msg = (
            f"Attempted to call '{self.name}' at character {self.position + 1}. "
            f"Failed because argument {index + 1} {reason}."
        )
        return MacroFunctionArgumentError(
            msg,
            failure_reason=MacroResolutionFailureReason.FUNCTION_ARGUMENT,
            function_name=self.name,
            position=position,
        )

    def error(self, reason: str) -> MacroFunctionArgumentError:
        msg = f"Attempted to call '{self.name}' at character {self.position + 1}. Failed because {reason}."
        return MacroFunctionArgumentError(
            msg,
            failure_reason=MacroResolutionFailureReason.FUNCTION_ARGUMENT,
            function_name=self.name,
            position=self.position,
        )

    def text(self, args: list[MacroValue], index: int) -> str:
        return render_value(args[index])

    def integer(self, args: list[MacroValue], index: int) -> int:
        value = self.number(args, index)
        if isinstance(value, float):
            if not value.is_integer():
                raise self.arg_error(index, f"must be a whole number, got '{render_value(value)}'")
            return int(value)
        return value

    def number(self, args: list[MacroValue], index: int) -> int | float:
        value = args[index]
        if isinstance(value, (int, float)):
            return value
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            parsed = float(text)
        except ValueError:
            raise self.arg_error(index, f"must be a number, got '{value}'") from None
        if not math.isfinite(parsed):
            raise self.arg_error(index, f"must be a finite number, got '{value}'")
        return parsed


class _CallParser:
    """Recursive-descent parser for one token's content."""

    def __init__(self, text: str, offset: int) -> None:
        self._text = text
        self._offset = offset
        self._pos = 0

    def parse_token(self) -> ParsedFunctionToken:
        self._skip_space()
        call = self._parse_call()
        self._skip_space()
        format_specs: list[FormatSpec] = []
        if self._pos < len(self._text):
            if self._text[self._pos] != ":":
                raise self._syntax_error(call.name, "unexpected text after the closing ')'")
            spec_text = self._text[self._pos + 1 :]
            try:
                format_specs = [parse_format_spec(part) for part in spec_text.split(":")]
            except MacroSyntaxError as exc:
                raise self._syntax_error(call.name, f"its format spec is invalid ({exc})") from exc
        return ParsedFunctionToken(call=call, format_specs=format_specs)

    def _parse_call(self) -> FunctionCall:
        start = self._pos
        name_match = _NAME.match(self._text, self._pos)
        if name_match is None:
            msg = "?"
            raise self._syntax_error(msg, "a function name was expected")
        name = name_match.group(0)
        self._pos = name_match.end()
        self._skip_space()
        if not self._consume("("):
            raise self._syntax_error(name, "'(' was expected after the function name")
        args: list[FunctionCall | VariableRef | Literal] = []
        self._skip_space()
        if self._consume(")"):
            return FunctionCall(name=name, args=args, position=self._offset + start)
        while True:
            args.append(self._parse_arg(name))
            self._skip_space()
            if self._consume(")"):
                return FunctionCall(name=name, args=args, position=self._offset + start)
            if not self._consume(","):
                raise self._syntax_error(name, "',' or ')' was expected between arguments")
            self._skip_space()

    def _parse_arg(self, function_name: str) -> FunctionCall | VariableRef | Literal:
        if self._pos >= len(self._text):
            raise self._syntax_error(function_name, "the closing ')' is missing")
        char = self._text[self._pos]
        if char in "\"'":
            return self._parse_string(function_name)
        number_match = _NUMBER.match(self._text, self._pos)
        if number_match is not None:
            start = self._pos
            self._pos = number_match.end()
            literal_text = number_match.group(0)
            if "." in literal_text:
                return Literal(value=float(literal_text), position=self._offset + start)
            return Literal(value=int(literal_text), position=self._offset + start)
        name_match = _NAME.match(self._text, self._pos)
        if name_match is None:
            raise self._syntax_error(function_name, f"'{char}' cannot start an argument")
        lookahead = self._pos + len(name_match.group(0))
        while lookahead < len(self._text) and self._text[lookahead].isspace():
            lookahead += 1
        if lookahead < len(self._text) and self._text[lookahead] == "(":
            return self._parse_call()
        start = self._pos
        self._pos = name_match.end()
        return VariableRef(name=name_match.group(0), position=self._offset + start)

    def _parse_string(self, function_name: str) -> Literal:
        start = self._pos
        quote = self._text[self._pos]
        self._pos += 1
        chars: list[str] = []
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if char == "\\" and self._pos + 1 < len(self._text) and self._text[self._pos + 1] in (quote, "\\"):
                chars.append(self._text[self._pos + 1])
                self._pos += 2
                continue
            if char == quote:
                self._pos += 1
                return Literal(value="".join(chars), position=self._offset + start)
            chars.append(char)
            self._pos += 1
        raise self._syntax_error(function_name, "a quoted text argument is missing its closing quote")

    def _skip_space(self) -> None:
        while self._pos < len(self._text) and self._text[self._pos].isspace():
            self._pos += 1

    def _consume(self, char: str) -> bool:
        if self._pos < len(self._text) and self._text[self._pos] == char:
            self._pos += 1
            return True
        return False

    def _syntax_error(self, function_name: str, reason: str) -> MacroFunctionSyntaxError:
        position = self._offset + self._pos
        msg = f"Attempted to read the call to '{function_name}' at character {position + 1}. Failed because {reason}."
        return MacroFunctionSyntaxError(
            msg,
            failure_reason=MacroResolutionFailureReason.FUNCTION_SYNTAX,
            function_name=function_name,
            position=position,
        )


class MacroFunctions:
    """The fixed function table and the entry points that parse and evaluate calls."""

    _TABLE: ClassVar[dict[str, _FunctionSpec]] = {}

    @classmethod
    def looks_like_call(cls, content: str) -> bool:
        """Whether a token's content starts like a function call (``name(``)."""
        return _LOOKS_LIKE_CALL.match(content) is not None

    @classmethod
    def parse(cls, content: str, offset: int = 0) -> ParsedFunctionToken | None:
        """Parse a token's content as a function call.

        Returns None when the content is not a call to the function table, so the caller treats
        the token the old way and leaves it verbatim: it does not start with ``name(``, or it names
        a function that is not in the table anywhere in the call (text such as ``{foo()}`` in JSX
        or ``{if(x) y}`` in a prompt). A malformed call to a known function raises
        MacroFunctionSyntaxError.

        Args:
            content: Text between the token's braces
            offset: Position of `content` within the full text, for error positions
        """
        head = _LOOKS_LIKE_CALL.match(content)
        if head is None:
            return None
        if head.group(1) not in cls._TABLE:
            return None
        parsed = _CallParser(content, offset).parse_token()
        if not cls._all_names_known(parsed.call):
            return None
        return parsed

    @classmethod
    def evaluate(cls, parsed: ParsedFunctionToken, context: MacroFunctionContext) -> str:
        """Evaluate a parsed token to its final text, format specs applied."""
        result = cls._evaluate_call(parsed.call, context)
        value: str | int
        if isinstance(result, float):
            value = render_value(result)
        else:
            value = result
        for format_spec in parsed.format_specs:
            try:
                value = format_spec.apply(value)
            except MacroResolutionError as exc:
                msg = (
                    f"Attempted to format the result of '{parsed.call.name}' at character "
                    f"{parsed.call.position + 1}. Failed because {exc}"
                )
                raise MacroFunctionArgumentError(
                    msg,
                    failure_reason=MacroResolutionFailureReason.FUNCTION_ARGUMENT,
                    function_name=parsed.call.name,
                    position=parsed.call.position,
                ) from exc
        return render_value(value)

    @classmethod
    def list_functions(cls) -> list[MacroFunctionInfo]:
        """Every function in the table, sorted by name, for pickers and docs."""
        return [
            MacroFunctionInfo(name=spec.name, signature=spec.signature, description=spec.description)
            for spec in sorted(cls._TABLE.values(), key=lambda spec: spec.name)
        ]

    @classmethod
    def register(cls, spec: _FunctionSpec) -> None:
        cls._TABLE[spec.name] = spec

    @classmethod
    def is_volatile(cls, name: str) -> bool:
        spec = cls._TABLE.get(name)
        if spec is None:
            return False
        return spec.volatile

    @classmethod
    def _all_names_known(cls, call: FunctionCall) -> bool:
        """Whether every function named anywhere in the call tree is in the table."""
        if call.name not in cls._TABLE:
            return False
        return all(cls._all_names_known(arg) for arg in call.args if isinstance(arg, FunctionCall))

    @classmethod
    def _evaluate_call(cls, call: FunctionCall, context: MacroFunctionContext) -> MacroValue:
        spec = cls._TABLE[call.name]
        count = len(call.args)
        if count < spec.min_args or (spec.max_args is not None and count > spec.max_args):
            msg = (
                f"Attempted to call '{call.name}' at character {call.position + 1}. "
                f"Failed because it takes {_describe_arity(spec)} but was given {count}. "
                f"Usage: {spec.signature}"
            )
            raise MacroFunctionArityError(
                msg,
                failure_reason=MacroResolutionFailureReason.FUNCTION_ARITY,
                function_name=call.name,
                position=call.position,
            )
        values = [cls._evaluate_arg(call, arg, context) for arg in call.args]
        site = _CallSite(
            name=call.name,
            position=call.position,
            context=context,
            arg_positions=[arg.position for arg in call.args],
        )
        return spec.impl(site, values)

    @classmethod
    def _evaluate_arg(
        cls, call: FunctionCall, arg: FunctionCall | VariableRef | Literal, context: MacroFunctionContext
    ) -> MacroValue:
        if isinstance(arg, Literal):
            return arg.value
        if isinstance(arg, FunctionCall):
            return cls._evaluate_call(arg, context)
        if arg.name not in context.variables:
            msg = (
                f"Attempted to call '{call.name}' at character {call.position + 1}. "
                f"Failed because there is no variable named '{arg.name}' (character {arg.position + 1})."
            )
            raise MacroFunctionArgumentError(
                msg,
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                function_name=call.name,
                position=arg.position,
                variable_name=arg.name,
            )
        return context.variables[arg.name]


def render_value(value: MacroValue) -> str:
    """Spell a function result as text. A whole float drops its ``.0`` so ``div(4, 2)`` is ``2``."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _call_is_volatile(call: FunctionCall) -> bool:
    if MacroFunctions.is_volatile(call.name):
        return True
    return any(isinstance(arg, FunctionCall) and _call_is_volatile(arg) for arg in call.args)


def _collect_variable_names(call: FunctionCall, names: set[str]) -> None:
    for arg in call.args:
        if isinstance(arg, VariableRef):
            names.add(arg.name)
        elif isinstance(arg, FunctionCall):
            _collect_variable_names(arg, names)


def _describe_arity(spec: _FunctionSpec) -> str:
    if spec.max_args is None:
        return f"{spec.min_args} or more arguments"
    if spec.min_args == spec.max_args:
        if spec.min_args == 1:
            return "1 argument"
        return f"{spec.min_args} arguments"
    return f"{spec.min_args} to {spec.max_args} arguments"


# ---------------------------------------------------------------------------
# Path functions
# ---------------------------------------------------------------------------


def _path_join(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    parts = [site.text(args, index) for index in range(len(args))]
    base = parts[0]
    for index, part in enumerate(parts[1:], start=1):
        if os.path.isabs(part) or Path(part).drive:
            msg = (
                f"Attempted to build a path with '{site.name}' at character {site.position + 1}. "
                f"Failed because argument {index + 1} ('{part}') is a full path, which would replace "
                "the folder before it. Only the first argument may be a full path."
            )
            raise MacroPathEscapeError(
                msg,
                failure_reason=MacroResolutionFailureReason.PATH_ESCAPE,
                function_name=site.name,
                position=site.arg_positions[index],
            )
    joined = os.path.normpath(os.path.join(*parts))
    normalized_base = os.path.normpath(base) if base else os.curdir
    relative = os.path.relpath(joined, normalized_base)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        msg = (
            f"Attempted to build a path with '{site.name}' at character {site.position + 1}. "
            f"Failed because '..' in the arguments climbs out of '{base}'."
        )
        raise MacroPathEscapeError(
            msg,
            failure_reason=MacroResolutionFailureReason.PATH_ESCAPE,
            function_name=site.name,
            position=site.position,
        )
    return joined


def _ext(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return os.path.splitext(site.text(args, 0))[1].removeprefix(".")


def _with_ext(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    path = site.text(args, 0)
    extension = site.text(args, 1).strip()
    if os.sep in extension or "/" in extension:
        raise site.arg_error(1, f"must be an extension like 'png', got '{extension}'")
    root = os.path.splitext(path)[0]
    if extension:
        result = f"{root}.{extension.removeprefix('.')}"
    else:
        result = root
    return result


def _basename(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return os.path.basename(os.path.normpath(site.text(args, 0)))


def _dirname(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return os.path.dirname(os.path.normpath(site.text(args, 0)))


def _stem(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return os.path.splitext(os.path.basename(os.path.normpath(site.text(args, 0))))[0]


def _temp_dir_path(site: _CallSite) -> str:
    value = site.context.variables.get(TEMP_DIR_VARIABLE)
    if not isinstance(value, str) or not value:
        msg = "no project temp folder is available (the 'temp' folder of the current project)"
        raise site.error(msg)
    return value


def _temp_dir(site: _CallSite, args: list[MacroValue]) -> MacroValue:  # noqa: ARG001
    return site.context.confine(site.name, site.position, _temp_dir_path(site))


def _temp_file(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    name = f"tmp_{uuid.uuid4().hex[:_TEMP_FILE_ID_LENGTH]}"
    if args:
        extension = site.text(args, 0).strip().removeprefix(".")
        if os.sep in extension or "/" in extension:
            raise site.arg_error(0, f"must be an extension like 'png', got '{extension}'")
        if extension:
            name = f"{name}.{extension}"
    result = os.path.join(_temp_dir_path(site), name)
    return site.context.confine(site.name, site.position, result)


# ---------------------------------------------------------------------------
# Time and randomness
# ---------------------------------------------------------------------------


def _now(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    pattern = _DEFAULT_NOW_FORMAT
    if args:
        pattern = site.text(args, 0)
    return datetime.now(UTC).astimezone().strftime(pattern)


def _date(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    pattern = _DEFAULT_DATE_FORMAT
    if args:
        pattern = site.text(args, 0)
    return datetime.now(UTC).astimezone().strftime(pattern)


def _random(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    low = 0
    high = _DEFAULT_RANDOM_MAX
    if len(args) == 1:
        high = site.integer(args, 0)
    elif len(args) == 2:  # noqa: PLR2004
        low = site.integer(args, 0)
        high = site.integer(args, 1)
    if low > high:
        msg = f"the lowest value ({low}) is greater than the highest ({high})"
        raise site.error(msg)
    return low + secrets.randbelow(high - low + 1)


def _random_text(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    length = _DEFAULT_RANDOM_TEXT_LENGTH
    if args:
        length = site.integer(args, 0)
    if length < 1 or length > _MAX_RANDOM_TEXT_LENGTH:
        raise site.arg_error(0, f"must be between 1 and {_MAX_RANDOM_TEXT_LENGTH}, got {length}")
    return "".join(secrets.choice(_RANDOM_TEXT_ALPHABET) for _ in range(length))


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _join(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    separator = site.text(args, 0)
    if len(args) == 2:  # noqa: PLR2004
        # One item: a list variable, which reaches substitution as one item per line.
        return separator.join(site.text(args, 1).splitlines())
    return separator.join(site.text(args, index) for index in range(1, len(args)))


def _format(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    template = site.text(args, 0)
    fill = [site.text(args, index) for index in range(1, len(args))]
    pieces = re.split(r"(%%|%s)", template)
    placeholder_count = sum(1 for piece in pieces if piece == "%s")
    if placeholder_count != len(fill):
        msg = f"the template has {placeholder_count} '%s' placeholder(s) but {len(fill)} value(s) were given"
        raise site.error(msg)
    out: list[str] = []
    fill_iter = iter(fill)
    for piece in pieces:
        if piece == "%%":
            out.append("%")
        elif piece == "%s":
            out.append(next(fill_iter))
        else:
            out.append(piece)
    return "".join(out)


def _upper(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return site.text(args, 0).upper()


def _lower(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return site.text(args, 0).lower()


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------


def _add(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return sum(site.number(args, index) for index in range(len(args)))


def _sub(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return site.number(args, 0) - site.number(args, 1)


def _mul(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return math.prod(site.number(args, index) for index in range(len(args)))


def _div(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    divisor = site.number(args, 1)
    if divisor == 0:
        raise site.arg_error(1, "is zero, and a number cannot be divided by zero")
    return site.number(args, 0) / divisor


def _round(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    value = site.number(args, 0)
    if len(args) == 1:
        return round(value)
    return round(value, site.integer(args, 1))


def _min(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return min(site.number(args, index) for index in range(len(args)))


def _max(site: _CallSite, args: list[MacroValue]) -> MacroValue:
    return max(site.number(args, index) for index in range(len(args)))


for _spec in (
    _FunctionSpec(
        "path_join", "path_join(folder, part, ...)", "Joins folder and file names into one path.", 1, None, _path_join
    ),
    _FunctionSpec("ext", "ext(path)", "The extension of a path, without the dot ('png').", 1, 1, _ext),
    _FunctionSpec("with_ext", "with_ext(path, ext)", "The path with its extension replaced.", 2, 2, _with_ext),
    _FunctionSpec("basename", "basename(path)", "The file name at the end of a path, with extension.", 1, 1, _basename),
    _FunctionSpec("dirname", "dirname(path)", "The folder that contains a path.", 1, 1, _dirname),
    _FunctionSpec("stem", "stem(path)", "The file name at the end of a path, without extension.", 1, 1, _stem),
    _FunctionSpec("temp_dir", "temp_dir()", "The current project's temp folder.", 0, 0, _temp_dir),
    _FunctionSpec(
        "temp_file",
        "temp_file(ext?)",
        "A new unique file path in the project's temp folder.",
        0,
        1,
        _temp_file,
        volatile=True,
    ),
    _FunctionSpec(
        "now",
        "now(format?)",
        "The current local date and time (default format %Y-%m-%d_%H-%M-%S).",
        0,
        1,
        _now,
        volatile=True,
    ),
    _FunctionSpec("date", "date(format?)", "Today's local date (default format %Y-%m-%d).", 0, 1, _date, volatile=True),
    _FunctionSpec(
        "random",
        "random(min?, max?)",
        "A random whole number from min to max, inclusive (default 0 to 100).",
        0,
        2,
        _random,
        volatile=True,
    ),
    _FunctionSpec(
        "random_text",
        "random_text(length?)",
        "Random lowercase letters and digits (default 8 characters).",
        0,
        1,
        _random_text,
        volatile=True,
    ),
    _FunctionSpec(
        "join",
        "join(separator, list_or_value, ...)",
        "Joins values, or the lines of a list variable, with a separator.",
        2,
        None,
        _join,
    ),
    _FunctionSpec(
        "format",
        "format(template, value, ...)",
        "Fills each %s in the template with the next value (%% for a literal %).",
        1,
        None,
        _format,
    ),
    _FunctionSpec("upper", "upper(text)", "The text in upper case.", 1, 1, _upper),
    _FunctionSpec("lower", "lower(text)", "The text in lower case.", 1, 1, _lower),
    _FunctionSpec("add", "add(a, b, ...)", "Adds numbers.", 2, None, _add),
    _FunctionSpec("sub", "sub(a, b)", "Subtracts b from a.", 2, 2, _sub),
    _FunctionSpec("mul", "mul(a, b, ...)", "Multiplies numbers.", 2, None, _mul),
    _FunctionSpec("div", "div(a, b)", "Divides a by b.", 2, 2, _div),
    _FunctionSpec("round", "round(number, digits?)", "Rounds to a whole number, or to digits places.", 1, 2, _round),
    _FunctionSpec("min", "min(a, b, ...)", "The smallest of the numbers.", 1, None, _min),
    _FunctionSpec("max", "max(a, b, ...)", "The largest of the numbers.", 1, None, _max),
):
    MacroFunctions.register(_spec)

__all__ = [
    "FunctionCall",
    "Literal",
    "MacroFunctionContext",
    "MacroFunctionError",
    "MacroFunctionInfo",
    "MacroFunctions",
    "MacroValue",
    "ParsedFunctionToken",
    "VariableRef",
    "render_value",
]
