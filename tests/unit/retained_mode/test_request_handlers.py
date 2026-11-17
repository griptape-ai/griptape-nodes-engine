import functools
import inspect
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles, scope_from_signature


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


class TestScopeFromSignature:
    def test_no_workflow_parameter_declares_none(self) -> None:
        def handler(request: _PingRequest) -> None:
            pass

        assert scope_from_signature(handler) is None

    def test_a_workflow_with_no_default_is_workflow(self) -> None:
        def handler(request: _PingRequest, workflow: object) -> None:
            pass

        assert scope_from_signature(handler) is RequestScope.WORKFLOW

    def test_a_workflow_defaulting_to_none_is_optional_workflow(self) -> None:
        def handler(request: _PingRequest, workflow: object | None = None) -> None:
            pass

        assert scope_from_signature(handler) is RequestScope.OPTIONAL_WORKFLOW

    def test_a_workflow_with_another_default_raises(self) -> None:
        def handler(request: _PingRequest, workflow: object = "mine") -> None:
            pass

        with pytest.raises(TypeError, match="defaults to 'mine'"):
            scope_from_signature(handler)

    @pytest.mark.parametrize("variadic", ["args", "kwargs"])
    def test_variadic_parameters_without_workflow_raise(self, variadic: str) -> None:
        """A wrapper that hides its handler's signature can't say whether the handler takes a workflow."""

        def with_args(request: _PingRequest, *args: Any) -> None:
            pass

        def with_kwargs(request: _PingRequest, **kwargs: Any) -> None:
            pass

        handler = with_args if variadic == "args" else with_kwargs
        with pytest.raises(TypeError, match=r"\*args or \*\*kwargs"):
            scope_from_signature(handler)

    def test_a_wrapper_declares_the_scope_of_the_handler_it_wraps(self) -> None:
        def handler(request: _PingRequest, workflow: object) -> None:
            pass

        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> None:
            return handler(*args, **kwargs)

        assert scope_from_signature(wrapper) is RequestScope.WORKFLOW

    def test_a_signature_attribute_declares_the_scope(self) -> None:
        def handler(request: _PingRequest, workflow: object | None = None) -> None:
            pass

        class _Wrapper:
            def __init__(self) -> None:
                self.__signature__ = inspect.signature(handler)

            def __call__(self, request: _PingRequest, **kwargs: Any) -> None:
                return handler(request, **kwargs)

        assert scope_from_signature(_Wrapper()) is RequestScope.OPTIONAL_WORKFLOW


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


class TestRegistrationReadsTheScope:
    def test_each_signature_registers_its_scope(self) -> None:
        class _Scoped:
            @handles(_PingRequest)
            def on_ping(self, request: _PingRequest, workflow: object) -> None:
                pass

            @handles(_PongRequest)
            def on_pong(self, request: _PongRequest, workflow: object | None = None) -> None:
                pass

        event_manager = EventManager()

        event_manager.register_request_handlers(_Scoped())

        assert event_manager.get_scope_for_request_type(_PingRequest) is RequestScope.WORKFLOW
        assert event_manager.get_scope_for_request_type(_PongRequest) is RequestScope.OPTIONAL_WORKFLOW

    def test_an_engine_handler_with_no_workflow_parameter_is_host(self) -> None:
        event_manager = EventManager()

        event_manager.register_request_handlers(_Base())

        assert event_manager.get_scope_for_request_type(_PingRequest) is RequestScope.HOST

    def test_without_workflow_none_leaves_a_handler_with_no_workflow_parameter_unscoped(self) -> None:
        """As for a library's handler, which keeps reading the ambient workflow."""

        def on_ping(request: _PingRequest) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        event_manager = EventManager()

        event_manager.assign_manager_to_request_type(_PingRequest, on_ping, without_workflow=None)

        assert event_manager.get_scope_for_request_type(_PingRequest) is None

    def test_a_signature_that_cannot_say_refuses_to_register(self) -> None:
        def on_ping(request: _PingRequest, workflow: object = "mine") -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        event_manager = EventManager()

        with pytest.raises(TypeError, match="defaults to 'mine'"):
            event_manager.assign_manager_to_request_type(_PingRequest, on_ping)
        assert event_manager.get_manager_for_request_type(_PingRequest) is None

    def test_removing_the_handler_forgets_its_scope(self) -> None:
        def on_ping(request: _PingRequest) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_PingRequest, on_ping)

        event_manager.remove_manager_from_request_type(_PingRequest)

        assert event_manager.get_scope_for_request_type(_PingRequest) is None


class TestReplaceManagerForRequestType:
    def test_keeps_the_registered_scope(self) -> None:
        event_manager = EventManager()

        def on_ping(request: _PingRequest, workflow: object) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        @functools.wraps(on_ping)
        def wrapper(request: _PingRequest, **kwargs: object) -> ResultPayload:
            return on_ping(request, **kwargs)

        event_manager.assign_manager_to_request_type(_PingRequest, on_ping)

        event_manager.replace_manager_for_request_type(_PingRequest, wrapper)

        assert event_manager.get_manager_for_request_type(_PingRequest) is wrapper
        assert event_manager.get_scope_for_request_type(_PingRequest) is RequestScope.WORKFLOW

    def test_refuses_a_callback_that_takes_a_different_workflow(self) -> None:
        def on_ping(request: _PingRequest, workflow: object) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        def no_workflow(request: _PingRequest) -> ResultPayload:  # noqa: ARG001
            return GenericResultFailure(result_details="unused")

        event_manager = EventManager()
        event_manager.assign_manager_to_request_type(_PingRequest, on_ping)

        with pytest.raises(TypeError, match="different `workflow` parameter"):
            event_manager.replace_manager_for_request_type(_PingRequest, no_workflow)
        assert event_manager.get_manager_for_request_type(_PingRequest) is on_ping

    def test_needs_a_registered_handler(self) -> None:
        with pytest.raises(ValueError, match="has none"):
            EventManager().replace_manager_for_request_type(_PingRequest, MagicMock())
