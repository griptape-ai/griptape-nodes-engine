"""Removes secrets and personal identifiers from anything bound for a diagnostics report.

A report exists to be sent to someone else, so everything in it passes through a ``Redactor``
first. Three things go: config values whose key says they hold a credential (keys kept, values
dropped), values the engine knows are secrets from a ``.env`` file, and anything shaped like a
credential plus the home directory and username.

Counts are recorded per reason, so a reader can tell "this setting is empty" from "this setting
was hidden from you".
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

# Keys whose values are credentials by construction rather than by name. `env` and `headers`
# come from MCPServerConfig, where the field exists to carry an API key to a server.
SENSITIVE_KEY_NAMES = frozenset({"env", "headers"})

# Keys naming which secrets the engine looks for. Special-cased rather than masked wholesale
# because the two accepted shapes differ: a list holds variable names, which the report
# publishes by design, while a mapping holds names and default values -- real credentials.
KEY_NAMES_DECLARING_SECRETS = frozenset({"secrets_to_register"})

# Substring match, not whole-word, so `api_key`, `openai_api_key` and `keys` all match.
# Over-matching (`keyboard`, `author`) only costs a hidden value, which is the safe failure.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(key|token|secret|password|passwd|pwd|credential|auth|cookie|signature|bearer)"
)

# A known secret shorter than this is not searched for in free text: a secret value of `1`
# or `dev` would have every occurrence replaced, destroying the logs to protect nothing.
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
    URL_CREDENTIALS = "url_credentials"
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

# A `bearer`/`basic` token shorter than this is left alone. The value class matches ordinary
# letters, so a low threshold redacts prose like "Bearer credentials required". Genuinely
# short opaque values are still covered by the known-secret and query-parameter rules.
MIN_BEARER_TOKEN_LENGTH = 16

# `Bearer <token>` in a logged header dump or an HTTP error.
_BEARER_PATTERN = TextPattern(
    RedactionReason.BEARER_TOKEN,
    re.compile(rf"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{{{MIN_BEARER_TOKEN_LENGTH},}}"),
    rf"\1 {REDACTED}",
)

# Query-string parameters whose value grants access on its own (a presigned URL is live until
# it expires). A name pattern, not a list, so vendor spellings match without being enumerated.
_SIGNED_URL_PARAMETER_WORDS = "key|token|secret|password|credential|signature|authorization"

# Abbreviations, matched only as the whole parameter name -- as substrings they hit
# `errorcode`, `design`, `author`. Kept because each is a real credential's real spelling
# (Azure SAS `sig`, OAuth `code`).
_SIGNED_URL_PARAMETER_ABBREVIATIONS = "auth|sig|code"

_SIGNED_URL_PARAMETER_NAME = (
    rf"(?:[^=&\s]*(?:{_SIGNED_URL_PARAMETER_WORDS})[^=&\s]*|(?:{_SIGNED_URL_PARAMETER_ABBREVIATIONS}))"
)

_SIGNED_URL_PATTERN = TextPattern(
    RedactionReason.SIGNED_URL_PARAMETER,
    re.compile(rf"(?i)([?&]{_SIGNED_URL_PARAMETER_NAME}=)[^&\s\"'<>]+"),
    rf"\1{REDACTED}",
)

# Password in a URL's userinfo (`scheme://user:password@host`). The password class allows `@`
# and excludes `/?#` and whitespace so the greedy match reaches the last `@` in the authority
# -- otherwise `postgres://u:p@ssw0rd@host` redacts only `p` and writes `ssw0rd` into the bundle.
_URL_CREDENTIALS_PATTERN = TextPattern(
    RedactionReason.URL_CREDENTIALS,
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^/?#\s]+@"),
    rf"\1:{REDACTED}@",
)

# Rules that consume a whole value up to a delimiter. They run first because their value
# classes stop at `<`, so an already-inserted `<redacted>` would truncate the match.
DELIMITED_VALUE_PATTERNS = [_BEARER_PATTERN, _SIGNED_URL_PATTERN, _URL_CREDENTIALS_PATTERN]

# What may follow a home directory for it to really be one: a home of `/Users/sam` would
# otherwise rewrite `/Users/samantha/x` to `~antha/x`. Punctuation only -- a letter or digit
# continues an identifier, so it was never this home. `-` is included (`/Users/sam-2`).
_HOME_DIRECTORY_BOUNDARY = r"""(?=[/\\_.\-,;:!?*|'")\]}>\s]|$)"""


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

        # Order matters: every rule that inserts `<redacted>` constrains what can run after it.
        # Delimiter-anchored first because their classes stop at `<`, then known secrets before the
        # shape-matching patterns can rewrite part of one, then identity last.
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

        Walks dicts and lists. Values under a credential-shaped key are replaced; every other
        string still goes through ``redact_text``, because a credential pasted into an innocently
        named setting is the case that would leak.
        """
        return self._redact_config_value(config, key=None)

    def counts(self) -> dict[str, int]:
        """Return the number of values removed, keyed by reason, omitting reasons that never fired."""
        return {str(reason): count for reason, count in sorted(self._counts.items()) if count > 0}

    def total_redactions(self) -> int:
        """Return the total number of values removed."""
        return sum(self._counts.values())

    def _redact_config_value(self, value: Any, key: str | None) -> Any:
        # Checked before the name heuristic: `secrets_to_register` matches `secret` below,
        # but only half of what it can hold is actually a credential.
        if key is not None and key.lower() in KEY_NAMES_DECLARING_SECRETS:
            return self._redact_declared_secrets(value)

        if key is not None and self._is_sensitive_key(key):
            return self._mask(value)

        if isinstance(value, dict):
            # Keys are redacted as well as values: a settings subtree can be keyed by absolute path.
            # Sensitivity is decided on the original key, so redacting it cannot change masking.
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

    def _redact_declared_secrets(self, value: Any) -> Any:
        """Keep the secret names a library declared, and drop any default values beside them.

        ``secrets_to_register`` is either a list of names or a mapping of name to default value.
        The names are the diagnostic signal and are not secret; a default value in the mapping
        form is a credential, so it is masked.
        """
        if isinstance(value, dict):
            return self._mask(value)

        return self._redact_config_value(value, key=None)

    def _mask(self, value: Any) -> Any:
        """Replace a credential value, keeping as much non-secret shape as is safe."""
        # An unset value is not a secret, and "this is unset" is often the answer support wants.
        # Returning it as-is also keeps it out of the counts, which then only mean something was hidden.
        if value is None:
            return value
        if isinstance(value, str | dict | list) and len(value) == 0:
            return value

        # Recursing rather than blanket-replacing so an entry that is itself unset stays visibly
        # unset: `OPENAI_API_KEY: ""` means "declared but never filled in".
        if isinstance(value, dict):
            # Names kept, values dropped: knowing which variables are set is the diagnostic signal. The
            # names still go through key redaction, since such a mapping can be keyed by absolute path.
            return {self._redact_key(entry_key): self._mask(entry) for entry_key, entry in value.items()}

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
            TextPattern(
                RedactionReason.HOME_DIRECTORY,
                re.compile(re.escape(spelling) + _HOME_DIRECTORY_BOUNDARY, re.IGNORECASE),
                "~",
            )
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

        Windows paths reach logs with either separator depending on whether pathlib or a string
        built them, plus the doubled-backslash spelling one takes through JSON. Longest first, so
        the more specific spelling wins when one is a prefix of another.
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
