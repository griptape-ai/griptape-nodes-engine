"""`Engine.request` and `Engine.try_request` return the success result a request declares."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import (
    DispatchFailure,
    RequestFailedError,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure


@dataclass(kw_only=True)
class _AddRequest(RequestPayload["_AddResultSuccess", "_AddResultFailure"]):
    a: int
    b: int


@dataclass(kw_only=True)
class _AddResultSuccess(ResultPayloadSuccess):
    total: int


@dataclass(kw_only=True)
class _AddResultFailure(ResultPayloadFailure):
    pass


@dataclass(kw_only=True)
class _SubAddRequest(_AddRequest):
    """Inherits the success result its parent declares."""


@dataclass(kw_only=True)
class _UntypedRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _OtherResultSuccess(ResultPayloadSuccess):
    pass


@dataclass(kw_only=True)
class _OtherResultFailure(ResultPayloadFailure):
    pass


@dataclass(kw_only=True)
class _SuccessOnlyRequest(RequestPayload["_AddResultSuccess"]):
    """Declares a success and no failure."""


@dataclass(kw_only=True)
class _HookRefusal(DispatchFailure):
    pass


class _RefusedError(Exception):
    pass


@dataclass(kw_only=True)
class _RefusedResultFailure(ResultPayloadFailure):
    def as_exception(self, request: RequestPayload) -> Exception:  # noqa: ARG002
        return _RefusedError(str(self.result_details))


TOTAL = 5


@pytest.fixture
def engine() -> Engine:
    """A fresh engine to register test handlers on."""
    return Engine()


class TestDeclaredTypes:
    def test_quoted_declarations_resolve_to_the_result_classes(self) -> None:
        assert _AddRequest.success_type() is _AddResultSuccess
        assert _AddRequest.failure_type() is _AddResultFailure

    def test_a_request_that_declares_only_a_success_accepts_any_failure(self) -> None:
        assert _SuccessOnlyRequest.failure_type() is ResultPayloadFailure

    def test_a_subclass_inherits_the_declaration(self) -> None:
        assert _SubAddRequest.success_type() is _AddResultSuccess

    def test_a_request_that_declares_nothing_returns_the_base_results(self) -> None:
        assert _UntypedRequest.success_type() is ResultPayloadSuccess
        assert _UntypedRequest.failure_type() is ResultPayloadFailure

    def test_a_declaration_naming_no_class_raises(self) -> None:
        @dataclass(kw_only=True)
        class _MisnamedRequest(RequestPayload["_NoSuchResult"]):  # pyright: ignore[reportUndefinedVariable]
            pass

        with pytest.raises(TypeError, match="_NoSuchResult"):
            _MisnamedRequest.success_type()


class TestRequest:
    def test_returns_the_declared_success(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda r: _AddResultSuccess(total=r.a + r.b, result_details="added")
        )

        result = engine.request(_AddRequest(a=2, b=3))

        assert result.total == TOTAL

    @pytest.mark.asyncio
    async def test_async_returns_the_declared_success(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda r: _AddResultSuccess(total=r.a + r.b, result_details="added")
        )

        result = await engine.arequest(_AddRequest(a=2, b=3))

        assert result.total == TOTAL

    def test_a_failure_raises_with_its_details_and_cause(self, engine: Engine) -> None:
        cause = ValueError("bad input")
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda _: _AddResultFailure(result_details="Attempted to add. Failed.", exception=cause)
        )

        with pytest.raises(RequestFailedError, match=r"Attempted to add\. Failed\.") as caught:
            engine.request(_AddRequest(a=2, b=3))

        assert isinstance(caught.value.result, _AddResultFailure)
        assert caught.value.__cause__ is cause

    def test_a_failure_type_can_raise_its_own_error(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _SuccessOnlyRequest, lambda _: _RefusedResultFailure(result_details="refused")
        )

        with pytest.raises(_RefusedError, match="refused"):
            engine.request(_SuccessOnlyRequest())

    def test_a_handler_that_raises_is_a_failure(self, engine: Engine) -> None:
        def explode(_: _AddRequest) -> _AddResultSuccess:
            msg = "boom"
            raise RuntimeError(msg)

        engine.event_manager.assign_manager_to_request_type(_AddRequest, explode)

        with pytest.raises(RequestFailedError) as caught:
            engine.request(_AddRequest(a=2, b=3))

        assert isinstance(caught.value.__cause__, RuntimeError)

    def test_a_success_of_another_type_raises(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda _: _OtherResultSuccess(result_details="wrong")
        )

        with pytest.raises(TypeError, match="_AddResultSuccess"):
            engine.request(_AddRequest(a=2, b=3))

    def test_an_untyped_request_returns_any_success(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _UntypedRequest, lambda _: _OtherResultSuccess(result_details="ok")
        )

        assert isinstance(engine.request(_UntypedRequest()), _OtherResultSuccess)


class TestTryRequest:
    def test_returns_the_declared_success(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda r: _AddResultSuccess(total=r.a + r.b, result_details="added")
        )

        result = engine.try_request(_AddRequest(a=2, b=3))

        assert isinstance(result, _AddResultSuccess)

    @pytest.mark.asyncio
    async def test_returns_the_failure_instead_of_raising(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda _: _AddResultFailure(result_details="no")
        )

        result = await engine.atry_request(_AddRequest(a=2, b=3))

        assert isinstance(result, _AddResultFailure)

    def test_a_handler_that_raises_returns_a_dispatch_failure(self, engine: Engine) -> None:
        def explode(_: _AddRequest) -> _AddResultSuccess:
            msg = "boom"
            raise RuntimeError(msg)

        engine.event_manager.assign_manager_to_request_type(_AddRequest, explode)

        result = engine.try_request(_AddRequest(a=2, b=3))

        assert isinstance(result, GenericResultFailure)
        assert isinstance(result.exception, RuntimeError)

    def test_a_hook_refusal_returns_its_dispatch_failure(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda r: _AddResultSuccess(total=r.a + r.b, result_details="added")
        )
        engine.event_manager.add_pre_dispatch_hook(lambda _request, _context: _HookRefusal(result_details="no"))

        result = engine.try_request(_AddRequest(a=2, b=3))

        assert isinstance(result, _HookRefusal)

    def test_a_failure_of_another_type_raises(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda _: _OtherResultFailure(result_details="wrong")
        )

        with pytest.raises(TypeError, match="_AddResultFailure"):
            engine.try_request(_AddRequest(a=2, b=3))

    def test_results_narrow_by_match(self, engine: Engine) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _AddRequest, lambda _: _AddResultFailure(result_details="no")
        )

        match engine.try_request(_AddRequest(a=2, b=3)):
            case _AddResultSuccess():
                outcome = "added"
            case _AddResultFailure():
                outcome = "failed"
            case DispatchFailure():
                outcome = "not handled"

        assert outcome == "failed"
