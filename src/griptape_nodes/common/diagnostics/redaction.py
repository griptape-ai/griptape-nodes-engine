"""Removes secrets and personal identifiers from anything bound for a diagnostics report.

A diagnostics report exists to be sent to someone else, so everything in it passes
through a ``Redactor`` first. Three things are removed:

- Config values whose key says they hold a credential (``api_key``, ``env``,
  ``headers``, ...). The keys are kept, only the values go.
- Values the engine knows are secrets, because it read them from a ``.env`` file.
  Removing them from free text is the only way to catch a credential that a
  library logged into an error message.
- Patterns that look like credentials regardless of where they came from, plus the
  home directory and username, which identify a person rather than a machine.

Redaction counts are recorded per reason so a report can state that something was
removed. Support reading "0 values redacted" versus "3 values redacted" is the
difference between "this setting is empty" and "this setting was hidden from you".
"""

from __future__ import annotations

import getpass
import logging
import re
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("griptape_nodes")

REDACTED = "<redacted>"
REDACTED_USER = "<user>"

# Keys whose values are credentials by construction rather than by name. `env` and
# `headers` come from MCPServerConfig, where the whole point of the field is to carry
# an API key to a server.
SENSITIVE_KEY_NAMES = frozenset({"env", "headers"})

# Keys whose values are the *names* of secrets rather than secrets. `secrets_to_register`
# is a list of variable names a library wants the engine to look for, and the report
# publishes those names elsewhere by design. Masking them would replace the one piece of
# diagnostic signal the setting carries, and would inflate the redaction count with
# removals of things that were never secret.
KEY_NAMES_HOLDING_SECRET_NAMES = frozenset({"secrets_to_register"})

# Substring match, not whole-word: `api_key`, `openai_api_key`, and `keys` must all
# match. Over-matching (`keyboard`, `monkey`) costs a hidden value in a report and is
# the right way to be wrong.
SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(key|token|secret|password|credential|authorization)")

# A known secret shorter than this is not searched for in free text. Someone whose
# secret value is `1`, `true`, or `dev` would otherwise have every occurrence of that
# string in their logs replaced, destroying the logs to protect nothing.
MIN_SEARCHABLE_SECRET_LENGTH = 8

# Same reasoning for usernames: a two-character username appears inside ordinary words.
MIN_SEARCHABLE_USERNAME_LENGTH = 3


class RedactionReason(StrEnum):
    """Why a value was removed. Reported as counts so removals are visible."""

    CONFIG_KEY = "config_key"
    KNOWN_SECRET_VALUE = "known_secret_value"  # noqa: S105 - a reason name, not a credential
    API_KEY_PATTERN = "api_key_pattern"
    BEARER_TOKEN = "bearer_token"  # noqa: S105 - a reason name, not a credential
    SIGNED_URL_PARAMETER = "signed_url_parameter"
    HOME_DIRECTORY = "home_directory"
    USERNAME = "username"


class TextPattern(NamedTuple):
    """A substitution applied to free text, tagged with why it fires."""

    reason: RedactionReason
    pattern: re.Pattern[str]
    replacement: str


# Vendor-prefixed API keys. Each prefix is kept in the output so support can tell which
# provider's credential was present without seeing it.
_API_KEY_PATTERNS = [
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), f"sk-ant-{REDACTED}"),
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), f"sk-{REDACTED}"),
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\bgsk_[A-Za-z0-9_\-]{8,}"), f"gsk_{REDACTED}"),
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}"), f"ghp_{REDACTED}"),
    TextPattern(
        RedactionReason.API_KEY_PATTERN, re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), f"github_pat_{REDACTED}"
    ),
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), f"xoxb-{REDACTED}"),
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), f"AKIA{REDACTED}"),
    TextPattern(RedactionReason.API_KEY_PATTERN, re.compile(r"\bhf_[A-Za-z0-9]{8,}"), f"hf_{REDACTED}"),
]

# A `bearer`/`basic` token shorter than this is left alone. The value class below matches
# ordinary letters, so a low threshold redacts English prose: "Bearer credentials
# required" and "Basic authentication is not supported" would both be counted as hidden
# credentials. Real bearer tokens are far longer than this, and a genuinely short opaque
# value is still covered by the known-secret and query-parameter rules.
MIN_BEARER_TOKEN_LENGTH = 16

# `Bearer <token>` in a logged header dump or an HTTP error.
_BEARER_PATTERN = TextPattern(
    RedactionReason.BEARER_TOKEN,
    re.compile(rf"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{{{MIN_BEARER_TOKEN_LENGTH},}}"),
    rf"\1 {REDACTED}",
)

# Query-string parameter names whose value grants access on its own. A presigned URL in a
# log is a working credential for as long as it has not expired, so the parameter names
# are kept and the values dropped.
#
# A name pattern rather than a fixed list of names, so it stays in step with
# SENSITIVE_KEY_PATTERN and covers a vendor parameter nobody thought to enumerate:
# `X-Amz-Signature`, `x-goog-credential`, `AWSAccessKeyId`, and `client_secret` all match.
# `sig` and `auth` are included as their own words because the URL spellings are
# abbreviated where config keys are not.
_SIGNED_URL_PARAMETER_WORDS = "key|token|secret|password|credential|auth|sig|code"

_SIGNED_URL_PATTERN = TextPattern(
    RedactionReason.SIGNED_URL_PARAMETER,
    re.compile(rf"(?i)([?&][^=&\s]*(?:{_SIGNED_URL_PARAMETER_WORDS})[^=&\s]*=)[^&\s\"'<>]+"),
    rf"\1{REDACTED}",
)

# Rules that consume a whole value up to a delimiter. These run before every other rule:
# their value classes stop at `<`, so an earlier rule that inserted `<redacted>` into the
# middle of the value would truncate the match and leave the tail of a live credential
# behind. Running them first also means one credential is counted once rather than twice.
DELIMITED_VALUE_PATTERNS = [_BEARER_PATTERN, _SIGNED_URL_PATTERN]

GENERIC_TEXT_PATTERNS = [*DELIMITED_VALUE_PATTERNS, *_API_KEY_PATTERNS]


class Redactor:
    """Applies every redaction rule and counts what it removed.

    One instance is used for a whole report so the counts cover it end to end. Not
    thread-safe: the counter is mutated on every call.
    """

    def __init__(self, *, secret_values: Iterable[str] = (), normalize_identity: bool = True) -> None:
        """Build a redactor.

        Args:
            secret_values: Values the engine knows to be secrets, so they can be found
                in free text. Pass the values from the ``.env`` files; they are used to
                build search patterns and are never stored in a report.
            normalize_identity: Whether to replace the home directory with ``~`` and the
                username with ``<user>``.
        """
        self._counts: Counter[str] = Counter()

        identity_patterns = []
        if normalize_identity:
            identity_patterns = self._build_identity_patterns()

        # Order matters, and every rule that inserts `<redacted>` constrains what can run
        # after it. Delimiter-anchored rules go first because their value classes stop at
        # `<`: a token that had already been partly replaced would truncate their match and
        # leave its tail behind. Known secrets go next, before the shape-matching patterns
        # get a chance to rewrite part of one into something the exact match would miss.
        # Identity last, so a home path inside an already-redacted value is moot.
        self._patterns = [
            *DELIMITED_VALUE_PATTERNS,
            *self._build_secret_patterns(secret_values),
            *_API_KEY_PATTERNS,
            *identity_patterns,
        ]

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every known secret, credential pattern, and identifier removed."""
        if not text:
            return text

        redacted = text
        for entry in self._patterns:
            redacted, substitutions = entry.pattern.subn(entry.replacement, redacted)
            self._counts[entry.reason] += substitutions
        return redacted

    def redact_path(self, path: Path | str) -> str:
        """Return a path as a string with the home directory and username removed."""
        return self.redact_text(str(path))

    def redact_config(self, config: Any) -> Any:
        """Return a copy of a config tree with credential values removed.

        Walks dicts and lists. Values under a credential-shaped key are replaced;
        every other string is still run through ``redact_text``, because a credential
        pasted into an innocently named setting is exactly the case that would leak.
        """
        return self._redact_config_value(config, key=None)

    def counts(self) -> dict[str, int]:
        """Return the number of values removed, keyed by reason, omitting reasons that never fired."""
        return {str(reason): count for reason, count in sorted(self._counts.items()) if count > 0}

    def total_redactions(self) -> int:
        """Return the total number of values removed."""
        return sum(self._counts.values())

    def _redact_config_value(self, value: Any, key: str | None) -> Any:
        if key is not None and self._is_sensitive_key(key):
            return self._mask(value)

        if isinstance(value, dict):
            # Keys are redacted as well as values. A library is free to add a settings
            # subtree keyed by absolute path, and a key is as good a place for a home
            # directory to hide as a value is. Sensitivity is decided on the original key,
            # so redacting it cannot change whether its value is masked.
            return {
                self._redact_key(entry_key): self._redact_config_value(entry, key=str(entry_key))
                for entry_key, entry in value.items()
            }

        if isinstance(value, list):
            # List items have no key of their own. Items that are dicts re-supply the
            # context through their own keys.
            return [self._redact_config_value(item, key=None) for item in value]

        if isinstance(value, str):
            return self.redact_text(value)

        return value

    def _mask(self, value: Any) -> Any:
        """Replace a credential value, keeping as much non-secret shape as is safe."""
        # An unset value is not a secret, and "this is unset" is often the answer support
        # is looking for. Returning it as-is also keeps it out of the redaction counts,
        # so the counts only ever mean "something real was hidden".
        if value is None:
            return value
        if isinstance(value, str | dict | list) and len(value) == 0:
            return value

        # Recursing rather than blanket-replacing so an entry that is itself unset stays
        # visibly unset. `OPENAI_API_KEY: ""` means "declared but never filled in", which
        # is a common cause of the failures this report is collected to explain.
        if isinstance(value, dict):
            # Names kept, values dropped: knowing which variables are set is the
            # diagnostic signal, and the names are not themselves secret.
            return {entry_key: self._mask(entry) for entry_key, entry in value.items()}

        if isinstance(value, list):
            return [self._mask(item) for item in value]

        self._counts[RedactionReason.CONFIG_KEY] += 1
        return REDACTED

    def _redact_key(self, key: Any) -> Any:
        """Return a dict key with identifiers removed, leaving non-string keys as they are."""
        if isinstance(key, str):
            return self.redact_text(key)
        return key

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        lowered = key.lower()

        # Checked first: `secrets_to_register` matches `secret` below, and its values are
        # names rather than credentials.
        if lowered in KEY_NAMES_HOLDING_SECRET_NAMES:
            return False

        if lowered in SENSITIVE_KEY_NAMES:
            return True
        return SENSITIVE_KEY_PATTERN.search(lowered) is not None

    @staticmethod
    def _build_secret_patterns(secret_values: Iterable[str]) -> list[TextPattern]:
        """Build exact-match patterns for values known to be secrets.

        Sorted longest first so a secret that contains a shorter secret is replaced
        whole, rather than being broken into an unrecognizable remainder.
        """
        searchable = {value for value in secret_values if len(value) >= MIN_SEARCHABLE_SECRET_LENGTH}
        ordered = sorted(searchable, key=len, reverse=True)
        return [
            TextPattern(RedactionReason.KNOWN_SECRET_VALUE, re.compile(re.escape(value)), REDACTED) for value in ordered
        ]

    @staticmethod
    def _build_identity_patterns() -> list[TextPattern]:
        """Build patterns replacing the home directory with ``~`` and the username with ``<user>``."""
        patterns = [
            TextPattern(RedactionReason.HOME_DIRECTORY, re.compile(re.escape(spelling), re.IGNORECASE), "~")
            for spelling in Redactor._home_directory_spellings()
        ]

        username = Redactor._current_username()
        # Checked after the home directory, whose spellings contain the username. A very
        # short username appears inside ordinary words, so it is left alone.
        if username is not None and len(username) >= MIN_SEARCHABLE_USERNAME_LENGTH:
            patterns.append(
                TextPattern(
                    RedactionReason.USERNAME,
                    re.compile(rf"\b{re.escape(username)}\b", re.IGNORECASE),
                    REDACTED_USER,
                )
            )

        return patterns

    @staticmethod
    def _home_directory_spellings() -> list[str]:
        """Return every spelling of the home directory that could appear in text.

        Windows paths reach logs with both separators depending on whether pathlib or a
        string built them, so both are matched, plus the doubled-backslash spelling a
        Windows path takes once something has been through JSON. Longest first, so the more
        specific spelling wins when one is a prefix of another.
        """
        try:
            home = Path.home()
        except RuntimeError:
            # No home directory resolvable (a service account, some containers). Nothing
            # to normalize, and this must not stop a report from being produced.
            logger.debug("Could not determine the home directory; paths will not be normalized.", exc_info=True)
            return []

        home_string = str(home)
        spellings = {home_string, home_string.replace("\\", "/"), home_string.replace("\\", "\\\\")}
        return sorted(spellings, key=len, reverse=True)

    @staticmethod
    def _current_username() -> str | None:
        """Return the current username, or None when it cannot be determined."""
        try:
            return getpass.getuser()
        except (OSError, KeyError):
            # getpass falls back through env vars to the password database; with neither
            # available it raises. Not knowing the username is not a failure.
            logger.debug("Could not determine the current username; it will not be normalized.", exc_info=True)
            return None
