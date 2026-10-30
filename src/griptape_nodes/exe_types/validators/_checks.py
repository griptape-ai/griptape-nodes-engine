"""Checks shared by validators of any kind of value, and the error every check returns.

`check_count` backs `MaxCount`; `limit_error` builds the `NodeError`, with its link to the provider's
limits page, that every check in this package returns; `input_subject` starts the message naming the
input. Checks for a particular kind of value live beside the validators that use them.
"""

from __future__ import annotations

from griptape_nodes.exe_types.node_error import NodeError, NodeErrorLink
from griptape_nodes.exe_types.validators.base import LIMITS_LINK_LABEL


def check_count(
    count: int, *, maximum: int, parameter: str, noun: str = "items", docs_url: str | None = None
) -> NodeError | None:
    """An error when more inputs are connected than the provider accepts.

    Args:
        count: How many inputs are connected.
        maximum: The most the provider accepts.
        parameter: The parameter's name as the UI shows it.
        noun: The plural of what is counted, such as "prompts".
        docs_url: The provider's page documenting the limit.
    """
    if count <= maximum:
        return None
    return limit_error(
        f"{count} {noun} are connected to '{parameter}' and the limit is {maximum}. Remove {count - maximum}.",
        docs_url,
    )


def limit_error(message: str, docs_url: str | None) -> NodeError:
    if not docs_url:
        return NodeError(message, links=None)
    return NodeError(message, links=[NodeErrorLink(label=LIMITS_LINK_LABEL, url=docs_url)])


def input_subject(parameter: str, *, noun: str, position: int | None = None) -> str:
    """The start of a message naming one input, such as "Prompt 2 in 'prompts'".

    Args:
        parameter: The parameter's name as the UI shows it.
        noun: What the input is, such as "prompt".
        position: The input's 1-based position in a list parameter, or None for a single input.
    """
    if position is None:
        return f"The {noun} in '{parameter}'"
    return f"{noun[:1].upper()}{noun[1:]} {position} in '{parameter}'"
