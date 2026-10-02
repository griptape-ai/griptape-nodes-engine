"""Turn a node failure into the ``NodeErrorDetails`` sent on ``NodeErrorEvent.error``."""

from __future__ import annotations

import ast
import logging

from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.retained_mode.events.base_events import ForwardedException, ForwardedNodeError
from griptape_nodes.retained_mode.events.node_error_details import (
    NodeErrorDetails,
    exception_display_message,
    qualified_type_name,
    sanitize_attachments,
)

logger = logging.getLogger(__name__)


class NodeExecutionError(RuntimeError):
    """Raised by ``NodeExecutor`` when an ``ExecuteNodeRequest`` fails.

    The message is the flattened text that ends up in ``NodeErrorEvent.error_message``. The node's
    own exceptions ride along so ``build_node_error_details`` can read them without parsing that
    text. Exactly one of these describes the failure:

    * ``validation_exceptions``: the node declined to run. No exception was raised, so there is no
      ``__cause__`` to read them from.
    * ``exception``: the node raised while running. Also chained as ``__cause__``.
    * Neither: the engine failed the request before the node ran, and ``result_details`` says why.
    """

    def __init__(
        self,
        message: str,
        *,
        result_details: str,
        exception: BaseException | None = None,
        validation_exceptions: list[Exception] | None = None,
    ) -> None:
        super().__init__(message)
        self.result_details = result_details
        self.exception = exception
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
    return _from_exception(node_name, error.exception)


def _from_validation(node_name: str, exceptions: list[Exception]) -> NodeErrorDetails:
    if not exceptions:
        # Both callers guard against an empty list. Kept so a future caller can't crash the
        # error-reporting path, and messages=[] still tells the editor the node never ran.
        logger.debug("Node '%s' reported a validation failure with no exceptions", node_name)
        return NodeErrorDetails(message="The node failed validation but did not say why.", messages=[])
    details = _from_exception(node_name, exceptions[0])
    details.messages = [_message(node_name, exception) for exception in exceptions]
    return details


def _from_exception(node_name: str, exc: BaseException) -> NodeErrorDetails:
    details = NodeErrorDetails(message=_message(node_name, exc))
    if isinstance(exc, ForwardedException):
        details.exception_type = exc.original_type
    else:
        details.exception_type = qualified_type_name(exc)
    if isinstance(exc, ForwardedNodeError):
        attachments = exc.attachments
    elif isinstance(exc, NodeError):
        attachments = sanitize_attachments(exc.fields, exc.response, exc.links)
    else:
        return details
    details.fields = attachments.fields
    details.response = attachments.response
    details.links = attachments.links
    return details


def _message(node_name: str, exc: BaseException) -> str:
    if isinstance(exc, ForwardedException):
        return _strip_node_name(node_name, _forwarded_message(exc))
    return _strip_node_name(node_name, exception_display_message(exc))


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
