"""Structured parts of a node failure, sent to the editor alongside the flattened message.

The wire type, the builders that fill it from a failure, and the limits the engine enforces on what
a ``NodeError`` attaches. The details are built where the node fails, which is the worker when the
node runs in one, and ride back on ``ExecuteNodeResultFailure.error`` like any other dataclass.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.node_error import NodeError, NodeErrorLink
from griptape_nodes.retained_mode.events.base_events import ForwardedException
from griptape_nodes.utils.exception_utils import readable_exception_message

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 16 * 1024
MAX_LINKS = 3
MAX_LINK_LABEL_CHARS = 80
ALLOWED_LINK_SCHEMES = ("http://", "https://")
# A link starting with "#" opens a place in the editor, such as "#settings-secrets?filter=MY_KEY".
EDITOR_LINK_PREFIX = "#"
RESPONSE_DROPPED_FIELD = "response_dropped"
# Longer messages are left as they are, so reading a printed response stays cheap.
MAX_PRINTED_RESPONSE_MESSAGE_CHARS = 2 * MAX_RESPONSE_BYTES
# How many "{" in a message are tried as the start of a printed response before giving up.
MAX_PRINTED_RESPONSE_STARTS = 8
# A line above a printed response is its label, and is dropped, only when it ends in ":" and is at
# most this many words, like "Full API response:". A longer line is a sentence and stays.
MAX_RESPONSE_LABEL_WORDS = 4


@dataclass
class NodeErrorDetails:
    message: str
    """What went wrong, in the node's words. No engine preamble, no node name prefix."""

    exception_type: str | None = None
    """Qualified type, e.g. "builtins.KeyError"."""

    messages: list[str] | None = None
    """One entry per exception when the node failed validation, even if there is only one."""

    fields: dict[str, str] = field(default_factory=dict)
    """Labelled values the user may need to quote to support: request_id, error_code, generation_id."""

    response: dict[str, Any] | None = None
    """Provider response body, JSON-serializable, size-capped. Body only, never headers."""

    links: list[NodeErrorLink] = field(default_factory=list)
    """Documentation the node author points to for this failure."""


@dataclass
class ErrorAttachments:
    """The optional parts of a ``NodeError``, after sanitizing."""

    fields: dict[str, str]
    response: dict[str, Any] | None
    links: list[NodeErrorLink]


@dataclass
class _PrintedResponse:
    """A response dict a node printed at the end of its message, split from the text before it."""

    message: str
    response: dict[str, Any]


def build_node_error_details(node_name: str, error: BaseException | list[Exception]) -> NodeErrorDetails:
    """Build the structured error for a node from the exception it raised.

    Args:
        node_name: The failing node. A leading ``"{node_name}: "`` is removed from each message,
            because node libraries often prefix their messages with the node's name and the event
            already carries it.
        error: The exception the node raised, or the list of exceptions it returned when it
            declined to run.
    """
    if isinstance(error, list):
        return _from_validation(node_name, error)
    return _from_exception(node_name, error)


def build_engine_error_details(node_name: str, message: str, cause: BaseException | None) -> NodeErrorDetails:
    """Build the structured error for a failure the engine described, such as a worker that stopped.

    The engine's message says more than the exception behind it, so it is kept. The exception still
    tells the reader what kind of failure it was.
    """
    exception_type = None
    if cause is not None:
        exception_type = _exception_type(cause)
    return NodeErrorDetails(message=_strip_node_name(node_name, message), exception_type=exception_type)


def sanitize_attachments(fields: Any, response: Any, links: Any) -> ErrorAttachments:
    """Keep what can be shown and serialized. Drop the rest with a debug log, never stringify it.

    A response dropped for its size leaves ``RESPONSE_DROPPED_FIELD`` in ``fields`` so the reader
    knows there was one.
    """
    clean_fields = _sanitize_fields(fields)
    clean_response = None
    if response is not None:
        clean_response = _sanitize_response(response)
        if clean_response is None and isinstance(response, dict):
            clean_fields[RESPONSE_DROPPED_FIELD] = "true"
    return ErrorAttachments(fields=clean_fields, response=clean_response, links=_sanitize_links(links))


def _from_validation(node_name: str, exceptions: list[Exception]) -> NodeErrorDetails:
    if not exceptions:
        # Every caller guards against an empty list. Kept so a future caller can't crash the
        # error-reporting path, and messages=[] still tells the editor the node never ran.
        logger.debug("Node '%s' reported a validation failure with no exceptions", node_name)
        return NodeErrorDetails(message="The node failed validation but did not say why.", messages=[])
    details = NodeErrorDetails(
        message=_message(node_name, exceptions[0]),
        exception_type=_exception_type(exceptions[0]),
        messages=[_message(node_name, exception) for exception in exceptions],
    )
    # Any exception in the list may be a NodeError, such as one linking to the missing secret.
    # On a clash the earlier exception wins: its field value, its response, its links first.
    for exception in exceptions:
        attachments = _attachments(exception)
        if attachments is None:
            continue
        for key, value in attachments.fields.items():
            details.fields.setdefault(key, value)
        if details.response is None:
            details.response = attachments.response
        for link in attachments.links:
            if len(details.links) < MAX_LINKS and link not in details.links:
                details.links.append(link)
    # The marker says no response could be shown, which is false once another exception's was kept.
    if details.response is not None:
        details.fields.pop(RESPONSE_DROPPED_FIELD, None)
    return details


def _from_exception(node_name: str, exc: BaseException) -> NodeErrorDetails:
    details = NodeErrorDetails(message=_message(node_name, exc), exception_type=_exception_type(exc))
    attachments = _attachments(exc)
    if attachments is not None:
        details.fields = attachments.fields
        details.response = attachments.response
        details.links = attachments.links
    # A response the node attached wins, even one dropped for its size.
    if details.response is not None or RESPONSE_DROPPED_FIELD in details.fields:
        return details
    printed = _split_printed_response(details.message)
    if printed is not None:
        details.message = printed.message
        details.response = printed.response
    return details


def _split_printed_response(message: str) -> _PrintedResponse | None:
    r"""Split off a response dict printed at the end of a message, with the label in front of it.

    Many nodes put the provider's response in their message with an f-string, which prints a Python
    dict, not JSON. ``ast.literal_eval`` reads that back exactly and only evaluates literals, so it
    is safe on error text.

    Returns None, so the message is kept as it is, when no dict runs to the end of the message, when
    the dict is empty or has nothing in front of it, or when it isn't a response ``NodeError`` could
    attach.

    Examples:
        "Processing failed.\n\nFull API response:\n{'status': 'ERRORED'}" splits into
        "Processing failed." and {"status": "ERRORED"}.

        "Error code: 400 - {'error': {'code': 'unsupported_value'}}" splits into "Error code: 400"
        and {"error": {"code": "unsupported_value"}}.
    """
    if len(message) > MAX_PRINTED_RESPONSE_MESSAGE_CHARS:
        return None
    starts = [index for index, char in enumerate(message) if char == "{"]
    for start in starts[:MAX_PRINTED_RESPONSE_STARTS]:
        response = _literal_dict(message[start:])
        if response is None:
            continue
        # The first "{" that reads as a dict is the outermost one. A dict inside it is part of the
        # response, never the response itself.
        if not _introduces_response(message[:start]):
            return None
        return _printed_response(message[:start], response)
    return None


def _introduces_response(text: str) -> bool:
    """Whether the text before a dict hands off to it with ":", "-", or a line break.

    A dict right after a word is part of something else, such as a model config that prints as
    ``CLIPTextConfig {...}``, so it stays in the message.
    """
    before = text.rstrip(" \t")
    return before.endswith((":", "-", "\n"))


def _printed_response(text: str, response: dict[str, Any]) -> _PrintedResponse | None:
    message = _strip_response_label(text)
    if not message:
        return None
    if _sanitize_response(response) is None:
        return None
    return _PrintedResponse(message=message, response=response)


def _literal_dict(text: str) -> dict[str, Any] | None:
    source = text.strip()
    try:
        expression = ast.parse(source, mode="eval").body
        value = ast.literal_eval(expression)
    # What parse and literal_eval raise for text that isn't a literal, or is one too deep to read.
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    # The parser skips a trailing "# ..." as a comment. That text belongs to the message, so the
    # dict doesn't run to the end of it.
    if ast.get_source_segment(source, expression) != source:
        return None
    if not isinstance(value, dict) or not value:
        return None
    return value


def _strip_response_label(text: str) -> str:
    """Remove what introduced the response: a label line like "Full API response:", or ": " or " - "."""
    if _ends_with_response_label(text):
        text = text.rstrip().rpartition("\n")[0]
    return text.rstrip().rstrip(":-").rstrip()


def _ends_with_response_label(text: str) -> bool:
    """Whether the response starts on its own line under a short line ending in ":", with text above."""
    if not text.rstrip(" \t").endswith("\n"):
        return False
    head, newline, last_line = text.rstrip().rpartition("\n")
    if not newline or not head.strip():
        return False
    label = last_line.strip()
    if not label.endswith(":"):
        return False
    return len(label.split()) <= MAX_RESPONSE_LABEL_WORDS


def _attachments(exc: BaseException) -> ErrorAttachments | None:
    if not isinstance(exc, NodeError):
        return None
    return sanitize_attachments(exc.fields, exc.response, exc.links)


def _exception_type(exc: BaseException) -> str:
    # A worker's exception that reached the engine without details, such as one behind an
    # engine-written failure, only knows its original type by name.
    if isinstance(exc, ForwardedException) and exc.original_type is not None:
        return exc.original_type
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _message(node_name: str, exc: BaseException) -> str:
    return _strip_node_name(node_name, readable_exception_message(exc))


def _strip_node_name(node_name: str, message: str) -> str:
    prefix = f"{node_name}: "
    if not message.startswith(prefix):
        return message
    return message[len(prefix) :]


def _sanitize_fields(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        if value is not None:
            logger.debug("Dropped node error fields of type %s; expected a dict", type(value).__name__)
        return {}
    fields: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str | int | float | bool):
            logger.debug("Dropped node error field %r; keys must be strings and values text or numbers", key)
            continue
        fields[key] = str(item)
    return fields


def _sanitize_response(value: Any) -> dict[str, Any] | None:
    """Return the response if it is a JSON-serializable dict under the size cap, otherwise None."""
    if not isinstance(value, dict):
        logger.debug("Dropped node error response of type %s; expected a dict", type(value).__name__)
        return None
    try:
        # allow_nan=False because NaN and Infinity are not JSON: the editor's JSON.parse would
        # reject the whole event, not just the response.
        serialized = json.dumps(value, allow_nan=False)
    # RecursionError from a deeply nested response. This runs while reporting a failure, so
    # letting it escape would replace the node's error with an engine crash.
    except (TypeError, ValueError, RecursionError):
        logger.debug("Dropped node error response that is not JSON-serializable", exc_info=True)
        return None
    if len(serialized.encode("utf-8")) > MAX_RESPONSE_BYTES:
        logger.debug("Dropped node error response over the %d byte cap", MAX_RESPONSE_BYTES)
        return None
    return value


def _sanitize_links(value: Any) -> list[NodeErrorLink]:
    if value is None:
        return []
    if not isinstance(value, list | tuple):
        logger.debug("Dropped node error links of type %s; expected a list", type(value).__name__)
        return []
    links: list[NodeErrorLink] = []
    for item in value:
        link = _clean_link(item)
        if link is None:
            continue
        if len(links) == MAX_LINKS:
            logger.debug("Dropped node error links past the first %d", MAX_LINKS)
            break
        links.append(link)
    return links


def _clean_link(item: Any) -> NodeErrorLink | None:
    if not isinstance(item, NodeErrorLink):
        logger.debug("Dropped node error link of type %s", type(item).__name__)
        return None
    if not isinstance(item.label, str) or not isinstance(item.url, str):
        logger.debug("Dropped node error link with a non-string label or url")
        return None
    url = item.url
    if not url.startswith(EDITOR_LINK_PREFIX) and not url.lower().startswith(ALLOWED_LINK_SCHEMES):
        logger.debug("Dropped node error link %r; only http, https, and editor (#) links are allowed", url)
        return None
    return NodeErrorLink(label=item.label[:MAX_LINK_LABEL_CHARS], url=url)
