"""Turn a node failure into the ``NodeErrorDetails`` sent on ``NodeErrorEvent.error``."""

from __future__ import annotations

import ast
import logging

from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.retained_mode.events.base_events import ForwardedException, ForwardedNodeError
from griptape_nodes.retained_mode.events.node_error_details import (
    MAX_LINKS,
    RESPONSE_DROPPED_FIELD,
    ErrorAttachments,
    NodeErrorDetails,
    qualified_type_name,
    sanitize_attachments,
)
from griptape_nodes.utils.exception_utils import readable_exception_message

logger = logging.getLogger(__name__)


class NodeExecutionError(RuntimeError):
    """Raised by ``NodeExecutor`` when an ``ExecuteNodeRequest`` fails.

    The message is the flattened text that ends up in ``NodeErrorEvent.error_message``. The node's
    own exceptions ride along so ``build_node_error_details`` can read them without parsing that
    text. Exactly one of these describes the failure:

    * ``validation_exceptions``: the node declined to run. No exception was raised, so there is no
      ``__cause__`` to read them from.
    * ``exception`` with ``exception_from_node``: the node raised while running, so the exception's
      message is in the node's words. Also chained as ``__cause__``.
    * ``exception`` without ``exception_from_node``: the engine wrote ``result_details`` for the
      user, such as when a worker stopped responding, and the exception is only the cause.
    * Neither: the engine failed the request before the node ran, and ``result_details`` says why.
    """

    def __init__(
        self,
        message: str,
        *,
        result_details: str,
        exception: BaseException | None = None,
        exception_from_node: bool = False,
        validation_exceptions: list[Exception] | None = None,
    ) -> None:
        super().__init__(message)
        self.result_details = result_details
        self.exception = exception
        self.exception_from_node = exception_from_node
        self.validation_exceptions = validation_exceptions or []


def build_node_error_details(node_name: str, error: BaseException | list[Exception]) -> NodeErrorDetails:
    """Build the structured error for a failed node.

    Args:
        node_name: The failing node. A leading ``"{node_name}: "`` is removed from each message,
            because node libraries often prefix their messages with the node's name and the event
            already carries it.
        error: The exception that reached the emit site, or the list ``validate_before_node_run``
            returned.
    """
    if isinstance(error, list):
        return _from_validation(node_name, error)
    if not isinstance(error, NodeExecutionError):
        return _from_exception(node_name, error)
    if error.validation_exceptions:
        return _from_validation(node_name, error.validation_exceptions)
    if error.exception is None:
        return NodeErrorDetails(message=_strip_node_name(node_name, error.result_details))
    if not error.exception_from_node:
        # The engine wrote result_details for the user, and they say more than the exception that
        # caused them. The exception still tells the reader what kind of failure it was.
        return NodeErrorDetails(
            message=_strip_node_name(node_name, error.result_details),
            exception_type=_exception_type(error.exception),
        )
    return _from_exception(node_name, error.exception)


def _from_validation(node_name: str, exceptions: list[Exception]) -> NodeErrorDetails:
    if not exceptions:
        # Both callers guard against an empty list. Kept so a future caller can't crash the
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
    if attachments is None:
        return details
    details.fields = attachments.fields
    details.response = attachments.response
    details.links = attachments.links
    return details


def _attachments(exc: BaseException) -> ErrorAttachments | None:
    if isinstance(exc, ForwardedNodeError):
        return exc.attachments
    if isinstance(exc, NodeError):
        return sanitize_attachments(exc.fields, exc.response, exc.links)
    return None


def _exception_type(exc: BaseException) -> str | None:
    if isinstance(exc, ForwardedException):
        return exc.original_type
    return qualified_type_name(exc)


def _message(node_name: str, exc: BaseException) -> str:
    if isinstance(exc, ForwardedException):
        return _strip_node_name(node_name, _forwarded_message(exc))
    return _strip_node_name(node_name, readable_exception_message(exc))


def _forwarded_message(exc: ForwardedException) -> str:
    """Undo ``KeyError``'s quoting for a ``KeyError`` that crossed the worker boundary.

    The worker sends ``str(exc)``, which for a ``KeyError`` is ``repr()`` of its argument.
    ``literal_eval`` reverses that exactly for a string, and only parses literals, never code.
    Anything else, such as ``KeyError(5)`` or several arguments, keeps its text.
    """
    message = str(exc)
    if exc.original_type != "builtins.KeyError":
        return message
    try:
        value = ast.literal_eval(message)
    except (ValueError, SyntaxError):
        return message
    if not isinstance(value, str):
        return message
    return value


def _strip_node_name(node_name: str, message: str) -> str:
    prefix = f"{node_name}: "
    if not message.startswith(prefix):
        return message
    return message[len(prefix) :]
