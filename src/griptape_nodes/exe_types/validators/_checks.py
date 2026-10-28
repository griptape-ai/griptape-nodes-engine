"""Checks shared by validators of any kind of value, and the error every check returns.

`check_count` backs `MaxCount`; `limit_error` builds the `NodeError`, with its link to the provider's
limits page, that every check in this package returns. The checks on media files are in
`_media_checks`.
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
        noun: The plural of what is counted, such as "images".
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
