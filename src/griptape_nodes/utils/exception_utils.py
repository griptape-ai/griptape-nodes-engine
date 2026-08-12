"""Utility functions for reading information off exceptions."""

from __future__ import annotations


def readable_exception_message(err: BaseException) -> str:
    """The message an exception carries, without KeyError's repr-quoting.

    ``str()`` on a ``KeyError`` reprs its argument, so a ``KeyError`` raised with a sentence
    renders wrapped in quotes. Use this wherever an exception's message is about to be shown to
    a user or embedded in a result's details, so a raised-with-a-sentence lookup error reads the
    same as every other error.

    Args:
        err: Exception to read a message off of

    Returns:
        The exception's message text.

    Examples:
        >>> readable_exception_message(KeyError("Node type 'Agent' not found in library 'Lib'"))
        "Node type 'Agent' not found in library 'Lib'"
        >>> readable_exception_message(ValueError("bad value"))
        'bad value'
    """
    if isinstance(err, KeyError) and len(err.args) == 1 and isinstance(err.args[0], str):
        return err.args[0]
    return str(err)
