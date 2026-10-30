from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handled_request_scope, handles


@dataclass(kw_only=True)
class _PingRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _PongRequest(RequestPayload):
    pass


class _Base:
    @handles(_PingRequest)
    def on_ping(self, request: _PingRequest) -> None:
        pass


class _Child(_Base):
    @handles(_PingRequest)
    def on_ping(self, request: _PingRequest) -> None:
        pass

    @handles(_PongRequest)
    def on_pong(self, request: _PongRequest) -> None:
        pass


class _UnmarkedOverride(_Base):
    def on_ping(self, request: _PingRequest) -> None:
        pass


class _RemarkedOverride(_Base):
    @handles(_PongRequest)
    def on_ping(self, request: RequestPayload) -> None:
        pass


class _Stacked:
    @handles(_PingRequest)
    @handles(_PongRequest)
    def on_either(self, request: RequestPayload) -> None:
        pass


class _Static:
    @staticmethod
    @handles(_PingRequest)
    def on_ping(request: _PingRequest) -> None:
        pass

    @classmethod
    @handles(_PongRequest)
    def on_pong(cls, request: _PongRequest) -> None:
        pass


class TestHandles:
    def test_bare_decorator_raises(self) -> None:
        with pytest.raises(TypeError, match="takes one or more request types"):

            class _Bare:
                @handles  # type: ignore[arg-type]
                def on_ping(self, request: _PingRequest) -> None:
                    pass

    def test_above_staticmethod_raises(self) -> None:
        with pytest.raises(TypeError, match="directly above `def`"):

            class _Misordered:
                @handles(_PingRequest)
                @staticmethod
                def on_ping(request: _PingRequest) -> None:
                    pass

    def test_records_the_declared_scope(self) -> None:
        class _Host:
            @handles(_PingRequest, scope=RequestScope.HOST)
            def on_ping(self, request: _PingRequest) -> None:
                pass

        assert handled_request_scope(_Host.on_ping) is RequestScope.HOST
        assert handled_request_scope(_Base.on_ping) is None

    def test_stacked_marks_with_different_scopes_raise(self) -> None:
        with pytest.raises(TypeError, match="same scope"):

            class _Conflicting:
                @handles(_PingRequest, scope=RequestScope.HOST)
                @handles(_PongRequest)
                def on_either(self, request: RequestPayload) -> None:
                    pass


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

    def test_remarked_override_replaces_parent_marks(self) -> None:
        event_manager = EventManager()
        owner = _RemarkedOverride()

        event_manager.register_request_handlers(owner)

        assert _PingRequest not in event_manager._request_type_to_manager
        assert event_manager._request_type_to_manager[_PongRequest] == owner.on_ping

    def test_registers_staticmethod_and_classmethod_handlers(self) -> None:
        event_manager = EventManager()
        owner = _Static()

        event_manager.register_request_handlers(owner)

        assert event_manager._request_type_to_manager[_PingRequest] == owner.on_ping
        assert event_manager._request_type_to_manager[_PongRequest] == owner.on_pong

    def test_second_owner_for_same_type_raises(self) -> None:
        event_manager = EventManager()
        event_manager.register_request_handlers(_Base())

        with pytest.raises(ValueError, match="already assigned"):
            event_manager.register_request_handlers(_Base())


class TestRegistrationChecksTheWorkflowParameter:
    def test_each_scope_registers_with_its_signature(self) -> None:
        class _Scoped:
            @handles(_PingRequest, scope=RequestScope.WORKFLOW)
            def on_ping(self, request: _PingRequest, workflow: object) -> None:
                pass

            @handles(_PongRequest, scope=RequestScope.OPTIONAL_WORKFLOW)
            def on_pong(self, request: _PongRequest, workflow: object | None = None) -> None:
                pass

        event_manager = EventManager()

        event_manager.register_request_handlers(_Scoped())

        assert event_manager.get_scope_for_request_type(_PingRequest) is RequestScope.WORKFLOW
        assert event_manager.get_scope_for_request_type(_PongRequest) is RequestScope.OPTIONAL_WORKFLOW

    @pytest.mark.parametrize(
        ("scope", "match"),
        [
            (RequestScope.WORKFLOW, "has no `workflow` parameter"),
            (RequestScope.OPTIONAL_WORKFLOW, "defaulting to None"),
        ],
    )
    def test_a_scope_that_takes_a_workflow_needs_the_parameter(self, scope: RequestScope, match: str) -> None:
        def on_ping(request: _PingRequest) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        with pytest.raises(TypeError, match=match):
            EventManager().assign_manager_to_request_type(_PingRequest, on_ping, scope=scope)

    def test_workflow_scope_refuses_a_default(self) -> None:
        def on_ping(request: _PingRequest, workflow: object = None) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        with pytest.raises(TypeError, match="has a default"):
            EventManager().assign_manager_to_request_type(_PingRequest, on_ping, scope=RequestScope.WORKFLOW)

    def test_host_refuses_a_workflow_parameter(self) -> None:
        def on_ping(request: _PingRequest, workflow: object | None = None) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        with pytest.raises(TypeError, match="has a `workflow` parameter"):
            EventManager().assign_manager_to_request_type(_PingRequest, on_ping, scope=RequestScope.HOST)

    def test_undeclared_registers_with_a_workflow_parameter(self) -> None:
        """A handler with no scope, such as a library's, is not checked: dispatch passes it the request alone."""

        def on_ping(request: _PingRequest, workflow: object) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        event_manager = EventManager()

        event_manager.assign_manager_to_request_type(_PingRequest, on_ping)

        assert event_manager.get_manager_for_request_type(_PingRequest) is on_ping
        assert event_manager.get_scope_for_request_type(_PingRequest) is None

    def test_removing_the_handler_forgets_its_scope(self) -> None:
        def on_ping(request: _PingRequest) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_PingRequest, on_ping, scope=RequestScope.HOST)

        event_manager.remove_manager_from_request_type(_PingRequest)

        assert event_manager.get_scope_for_request_type(_PingRequest) is None


class TestReplaceManagerForRequestType:
    def test_keeps_the_registered_scope(self) -> None:
        event_manager = EventManager()

        def on_ping(request: _PingRequest, workflow: object) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        def wrapper(request: _PingRequest, **kwargs: object) -> ResultPayload:
            return on_ping(request, **kwargs)

        event_manager.assign_manager_to_request_type(_PingRequest, on_ping, scope=RequestScope.WORKFLOW)

        event_manager.replace_manager_for_request_type(_PingRequest, wrapper)

        assert event_manager.get_manager_for_request_type(_PingRequest) is wrapper
        assert event_manager.get_scope_for_request_type(_PingRequest) is RequestScope.WORKFLOW

    def test_needs_a_registered_handler(self) -> None:
        with pytest.raises(ValueError, match="has none"):
            EventManager().replace_manager_for_request_type(_PingRequest, MagicMock())
