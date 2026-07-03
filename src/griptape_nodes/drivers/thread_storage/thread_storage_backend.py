"""Thread storage backend enumeration."""

from enum import StrEnum


class ThreadStorageBackend(StrEnum):
    """Enumeration of available thread storage backends.

    Only ``LOCAL`` exists today. A Griptape Cloud backend lived here before the
    Pydantic AI migration and will return as a new member once GTC can persist
    opaque ``ModelMessage`` blobs.
    """

    LOCAL = "local"
