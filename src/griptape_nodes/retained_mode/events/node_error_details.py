"""Structured parts of a node failure, sent to the editor alongside the flattened message.

The wire type and the limits the engine enforces on what a ``NodeError`` attaches. This module
imports nothing from the other event modules: ``event_converter`` uses it to serialize an exception
for the worker boundary, and ``base_events`` imports ``event_converter``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.node_error import NodeErrorLink

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 16 * 1024
MAX_LINKS = 3
MAX_LINK_LABEL_CHARS = 80
ALLOWED_LINK_SCHEMES = ("http://", "https://")
# A link starting with "#" opens a place in the editor, such as "#settings-secrets?filter=MY_KEY".
EDITOR_LINK_PREFIX = "#"
RESPONSE_DROPPED_FIELD = "response_dropped"


@dataclass
class NodeErrorDetails:
    message: str
    """What went wrong, in the node's words. No engine preamble, no node name prefix."""

    exception_type: str | None = None
    """Qualified type, e.g. "builtins.KeyError". From type(exc) or ForwardedException.original_type."""

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


def exception_display_message(exc: BaseException) -> str:
    """Return the exception's message the way a person would write it.

    ``KeyError.__str__`` returns the repr of its argument, so ``str(KeyError("x"))`` is ``'x'``
    with quotes. Every other built-in returns the argument as written.

    Only an exact ``KeyError`` is unquoted. A subclass may define its own ``__str__``, and the worker
    path can only recognize ``builtins.KeyError`` by name, so matching subclasses here would make
    the two paths disagree.
    """
    if type(exc) is KeyError and len(exc.args) == 1 and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


def qualified_type_name(exc: BaseException) -> str:
    """Return the exception's type as ``module.QualName``, e.g. ``builtins.KeyError``."""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


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
        link = _coerce_link(item)
        if link is None:
            continue
        if len(links) == MAX_LINKS:
            logger.debug("Dropped node error links past the first %d", MAX_LINKS)
            break
        links.append(link)
    return links


def _coerce_link(item: Any) -> NodeErrorLink | None:
    """Accept a ``NodeErrorLink``, or the ``{label, url}`` dict it arrives as from a worker."""
    if isinstance(item, NodeErrorLink):
        label = item.label
        url = item.url
    elif isinstance(item, dict):
        label = item.get("label")
        url = item.get("url")
    else:
        logger.debug("Dropped node error link of type %s", type(item).__name__)
        return None
    if not isinstance(label, str) or not isinstance(url, str):
        logger.debug("Dropped node error link with a non-string label or url")
        return None
    if not url.startswith(EDITOR_LINK_PREFIX) and not url.lower().startswith(ALLOWED_LINK_SCHEMES):
        logger.debug("Dropped node error link %r; only http, https, and editor (#) links are allowed", url)
        return None
    return NodeErrorLink(label=label[:MAX_LINK_LABEL_CHARS], url=url)
