"""Utility functions for reading information off exceptions."""

from __future__ import annotations


def readable_exception_message(err: BaseException) -> str:
    """The message an exception carries, without KeyError's repr-quoting.

    ``str()`` on a ``KeyError`` reprs its argument, so a ``KeyError`` raised with a sentence renders
    wrapped in quotes. Use this at boundaries that render exceptions the engine did not raise, such
    as the ``except Exception`` around a node's ``__init__``: a node comes from a separately
    versioned library, and its lookups can raise a sentence-carrying ``KeyError`` this engine has no
    say over. Engine code that raises its own lookup failures should raise a ``KeyError`` subclass
    that renders plainly instead (see ``LibraryRegistryError``), so every handler benefits without
    calling this.

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
