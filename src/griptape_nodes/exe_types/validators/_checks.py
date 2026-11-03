"""Checks shared by validators of any kind of value, and the error every check returns.

`check_count` backs `MaxCount`; `limit_error` builds the `NodeError`, with its link to the provider's
limits page, that every check in this package returns; `input_subject` starts the message naming the
input, and `input_noun` what it calls the input. Checks for a particular kind of value live beside the
validators that use them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_error import NodeError, NodeErrorLink
from griptape_nodes.exe_types.validators.base import LIMITS_LINK_LABEL
from griptape_nodes.exe_types.validators.context import media_kind_of

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.utils.media_values import MediaKind


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
    """The error for an input outside a limit, linking the provider's limits page when there is one.

    Args:
        message: What's wrong and how to fix it, naming the input.
        docs_url: The provider's page documenting the limit, usually the node's `input_limits_url()`.
    """
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


# What an input is called in a message, by the kind of media its parameter holds.
_NOUN_FOR_KIND: dict[MediaKind, str] = {"image": "image", "video": "video", "audio": "audio clip"}
_DEFAULT_NOUN = "input"


def input_noun(parameter: Parameter) -> str:
    """What an input is called in a message: "image", "video" or "audio clip", or "input" for anything else."""
    kind = media_kind_of(parameter)
    if kind is None:
        return _DEFAULT_NOUN
    return _NOUN_FOR_KIND[kind]
