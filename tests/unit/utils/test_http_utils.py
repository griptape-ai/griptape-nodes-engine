from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

from griptape_nodes.utils.http_utils import RETRY_MAX_ATTEMPTS, is_retryable_httpx_error, request_with_retry

EXPECTED_CALLS_AFTER_ONE_FAILURE = 2


class TestIsRetryableHttpxError:
    """Tests for is_retryable_httpx_error predicate."""

    def test_connect_error_is_retryable(self) -> None:
        exc = httpx2.ConnectError("Connection refused")
        assert is_retryable_httpx_error(exc) is True

    def test_timeout_exception_is_retryable(self) -> None:
        exc = httpx2.TimeoutException("Timed out")
        assert is_retryable_httpx_error(exc) is True

    def test_read_timeout_is_retryable(self) -> None:
        exc = httpx2.ReadTimeout("Read timed out")
        assert is_retryable_httpx_error(exc) is True

    def test_500_error_is_retryable(self) -> None:
        response = httpx2.Response(500, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Server Error", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is True

    def test_502_error_is_retryable(self) -> None:
        response = httpx2.Response(502, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Bad Gateway", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is True

    def test_503_error_is_retryable(self) -> None:
        response = httpx2.Response(503, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Service Unavailable", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is True

    def test_400_error_is_not_retryable(self) -> None:
        response = httpx2.Response(400, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Bad Request", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is False

    def test_401_error_is_not_retryable(self) -> None:
        response = httpx2.Response(401, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Unauthorized", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is False

    def test_404_error_is_not_retryable(self) -> None:
        response = httpx2.Response(404, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Not Found", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is False

    def test_429_error_is_not_retryable(self) -> None:
        response = httpx2.Response(429, request=httpx2.Request("GET", "https://example.com"))
        exc = httpx2.HTTPStatusError("Too Many Requests", request=response.request, response=response)
        assert is_retryable_httpx_error(exc) is False

    def test_generic_exception_is_not_retryable(self) -> None:
        exc = ValueError("something went wrong")
        assert is_retryable_httpx_error(exc) is False

    def test_runtime_error_is_not_retryable(self) -> None:
        exc = RuntimeError("unexpected")
        assert is_retryable_httpx_error(exc) is False


# Mirrors retry_on_transient_error but with no wait between retries.
_retry_no_wait = retry(
    retry=retry_if_exception(is_retryable_httpx_error),
    wait=wait_none(),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    reraise=True,
)


class TestRetryOnTransientError:
    """Tests for the retry_on_transient_error decorator."""

    def test_succeeds_without_retry(self) -> None:
        call_count = 0

        @_retry_no_wait
        def succeed() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_500_then_succeeds(self) -> None:
        call_count = 0

        @_retry_no_wait
        def fail_once_then_succeed() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                response = httpx2.Response(500, request=httpx2.Request("GET", "https://example.com"))
                msg = "Server Error"
                raise httpx2.HTTPStatusError(msg, request=response.request, response=response)
            return "ok"

        result = fail_once_then_succeed()
        assert result == "ok"
        assert call_count == EXPECTED_CALLS_AFTER_ONE_FAILURE

    def test_retries_on_connect_error_then_succeeds(self) -> None:
        call_count = 0

        @_retry_no_wait
        def fail_once_then_succeed() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "Connection refused"
                raise httpx2.ConnectError(msg)
            return "ok"

        result = fail_once_then_succeed()
        assert result == "ok"
        assert call_count == EXPECTED_CALLS_AFTER_ONE_FAILURE

    def test_retries_on_timeout_then_succeeds(self) -> None:
        call_count = 0

        @_retry_no_wait
        def fail_once_then_succeed() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "Timed out"
                raise httpx2.TimeoutException(msg)
            return "ok"

        result = fail_once_then_succeed()
        assert result == "ok"
        assert call_count == EXPECTED_CALLS_AFTER_ONE_FAILURE

    def test_does_not_retry_on_404(self) -> None:
        call_count = 0

        @_retry_no_wait
        def always_404() -> str:
            nonlocal call_count
            call_count += 1
            response = httpx2.Response(404, request=httpx2.Request("GET", "https://example.com"))
            msg = "Not Found"
            raise httpx2.HTTPStatusError(msg, request=response.request, response=response)

        with pytest.raises(httpx2.HTTPStatusError):
            always_404()
        assert call_count == 1

    def test_gives_up_after_max_attempts(self) -> None:
        call_count = 0

        @_retry_no_wait
        def always_500() -> str:
            nonlocal call_count
            call_count += 1
            response = httpx2.Response(500, request=httpx2.Request("GET", "https://example.com"))
            msg = "Server Error"
            raise httpx2.HTTPStatusError(msg, request=response.request, response=response)

        with pytest.raises(httpx2.HTTPStatusError):
            always_500()
        assert call_count == RETRY_MAX_ATTEMPTS

    def test_does_not_retry_on_generic_exception(self) -> None:
        call_count = 0

        @_retry_no_wait
        def always_fails() -> str:
            nonlocal call_count
            call_count += 1
            msg = "unexpected"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="unexpected"):
            always_fails()
        assert call_count == 1


CUSTOM_MAX_ATTEMPTS = 5
HTTP_OK = 200


class TestRequestWithRetry:
    """Tests for the request_with_retry convenience function."""

    def test_succeeds_on_first_try(self) -> None:
        call_count = 0

        def mock_request(method: str, url: str, **_kwargs: Any) -> httpx2.Response:
            nonlocal call_count
            call_count += 1
            return httpx2.Response(200, request=httpx2.Request(method, url))

        with patch("httpx2.request", side_effect=mock_request):
            response = request_with_retry("GET", "https://example.com", wait=wait_none())
        assert response.status_code == HTTP_OK
        assert call_count == 1

    def test_retries_on_500(self) -> None:
        call_count = 0

        def mock_request(method: str, url: str, **_kwargs: Any) -> httpx2.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                response = httpx2.Response(500, request=httpx2.Request(method, url))
                response.raise_for_status()
            return httpx2.Response(200, request=httpx2.Request(method, url))

        with patch("httpx2.request", side_effect=mock_request):
            response = request_with_retry("GET", "https://example.com", wait=wait_none())
        assert response.status_code == HTTP_OK
        assert call_count == EXPECTED_CALLS_AFTER_ONE_FAILURE

    def test_custom_max_attempts(self) -> None:
        call_count = 0

        def mock_request(method: str, url: str, **_kwargs: Any) -> httpx2.Response:
            nonlocal call_count
            call_count += 1
            response = httpx2.Response(500, request=httpx2.Request(method, url))
            response.raise_for_status()
            return response  # unreachable, but required for type checker

        with patch("httpx2.request", side_effect=mock_request), pytest.raises(httpx2.HTTPStatusError):
            request_with_retry("GET", "https://example.com", max_attempts=CUSTOM_MAX_ATTEMPTS, wait=wait_none())
        assert call_count == CUSTOM_MAX_ATTEMPTS

    def test_passes_kwargs_through(self) -> None:
        captured_kwargs: dict[str, Any] = {}

        def mock_request(method: str, url: str, **kwargs: Any) -> httpx2.Response:
            captured_kwargs.update(kwargs)
            return httpx2.Response(200, request=httpx2.Request(method, url))

        with patch("httpx2.request", side_effect=mock_request):
            request_with_retry(
                "POST",
                "https://example.com",
                wait=wait_none(),
                json={"key": "value"},
                headers={"Authorization": "Bearer token"},
            )
        assert captured_kwargs["json"] == {"key": "value"}
        assert captured_kwargs["headers"] == {"Authorization": "Bearer token"}

    def test_should_retry_decides_what_is_retried(self) -> None:
        responses = [
            httpx2.Response(429, request=httpx2.Request("POST", "https://example.com")),
            httpx2.Response(200, request=httpx2.Request("POST", "https://example.com")),
        ]

        def retry_429(exc: BaseException) -> bool:
            return isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code == 429  # noqa: PLR2004

        with patch("griptape_nodes.utils.http_utils.httpx2.request", side_effect=responses) as request:
            response = request_with_retry("POST", "https://example.com", wait=wait_none(), should_retry=retry_429)

        assert response.status_code == HTTP_OK
        assert request.call_count == EXPECTED_CALLS_AFTER_ONE_FAILURE
