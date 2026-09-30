from dataclasses import dataclass

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.request_handlers import handles


@dataclass(kw_only=True)
class _PingRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _PongRequest(RequestPayload):
    pass


class _Base:
    @handles(_PingRequest)
    def on_ping(self, request: _PingRequest) -> ResultPayload: ...


class _Child(_Base):
    @handles(_PingRequest)
    def on_ping(self, request: _PingRequest) -> ResultPayload: ...

    @handles(_PongRequest)
    def on_pong(self, request: _PongRequest) -> ResultPayload: ...


class _UnmarkedOverride(_Base):
    def on_ping(self, request: _PingRequest) -> ResultPayload: ...


class _Stacked:
    @handles(_PingRequest)
    @handles(_PongRequest)
    def on_either(self, request: RequestPayload) -> ResultPayload: ...


class TestRegisterRequestHandlers:
    def test_registers_bound_methods_for_each_marked_type(self) -> None:
        event_manager = EventManager()
        owner = _Stacked()

        event_manager.register_request_handlers(owner)

        assert event_manager._request_type_to_manager[_PingRequest] == owner.on_either
        assert event_manager._request_type_to_manager[_PongRequest] == owner.on_either

    def test_subclass_override_registers_once(self) -> None:
        event_manager = EventManager()
        owner = _Child()

        event_manager.register_request_handlers(owner)

        assert event_manager._request_type_to_manager[_PingRequest] == owner.on_ping
        assert event_manager._request_type_to_manager[_PongRequest] == owner.on_pong

    def test_unmarked_override_inherits_parent_mark(self) -> None:
        event_manager = EventManager()
        owner = _UnmarkedOverride()

        event_manager.register_request_handlers(owner)

        assert event_manager._request_type_to_manager[_PingRequest] == owner.on_ping

    def test_second_owner_for_same_type_raises(self) -> None:
        event_manager = EventManager()
        event_manager.register_request_handlers(_Base())

        with pytest.raises(ValueError, match="already assigned"):
            event_manager.register_request_handlers(_Base())
