from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from typing_extensions import TypeVar

from griptape_nodes.retained_mode.events.base_events import Payload, RequestPayload
from griptape_nodes.serialization.converter import converter
from griptape_nodes.serialization.values import ValueEncodeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from cattrs import Converter

T = TypeVar("T", bound=Payload, default=Payload)


class PayloadRegistry:
    """Registry for payload types."""

    _registry: ClassVar[dict[str, type[Payload]]] = {}

    @classmethod
    def register(cls, payload_class: type[T]) -> type[T]:
        """Register a payload type.

        Args:
            payload_class: The payload class to register

        Returns:
            The registered class (for decorator use)
        """
        cls._registry[payload_class.__name__] = payload_class
        return payload_class

    @classmethod
    def get_type(cls, type_name: str) -> type[Payload] | None:
        """Get a payload type by name.

        Args:
            type_name: Name of the payload type

        Returns:
            The payload class or None if not found
        """
        return cls._registry.get(type_name)

    @classmethod
    def get_registry(cls) -> dict:
        """Get the full registry.

        Returns:
            Dictionary of payload type name to payload class
        """
        return cls._registry.copy()

    # Register decorator as a classmethod
    @classmethod
    def register_payload(cls, payload_class: type[T]) -> type[T]:
        """Decorator to register a payload type."""
        return cls.register(payload_class)


# A field typed `RequestPayload`, such as a node's `element_modification_commands`, can hold any
# registered request, so each one is sent with its registered name, the form `EventRequestBatch`
# takes: {"request_type": "AddParameterToNodeRequest", "request": {...}}.
def _make_request_unstructure_fn(_cls: type, conv: Converter) -> Callable[[RequestPayload], dict[str, Any]]:
    def unstructure(request: RequestPayload) -> dict[str, Any]:
        request_type = type(request)
        if PayloadRegistry.get_type(request_type.__name__) is not request_type:
            msg = f"A '{request_type.__name__}' request is not registered, so it could not be read back."
            raise ValueEncodeError(msg)
        return {"request_type": request_type.__name__, "request": conv.unstructure(request, request_type)}

    return unstructure


def _make_request_structure_fn(_cls: type, conv: Converter) -> Callable[[dict[str, Any], type], RequestPayload]:
    def structure(data: dict[str, Any], _type: type) -> RequestPayload:
        request_type = PayloadRegistry.get_type(data["request_type"])
        if request_type is None or not issubclass(request_type, RequestPayload):
            msg = f"'{data['request_type']}' is not a registered request."
            raise ValueError(msg)
        return conv.structure(data["request"], request_type)

    return structure


converter.register_unstructure_hook_factory(lambda cls: cls is RequestPayload, _make_request_unstructure_fn)
converter.register_structure_hook_factory(lambda cls: cls is RequestPayload, _make_request_structure_fn)
