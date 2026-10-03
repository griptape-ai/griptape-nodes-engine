import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx2
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.wait import WaitBaseT

logger = logging.getLogger("griptape_nodes")

RETRY_MAX_ATTEMPTS = 3
RETRY_WAIT_MULTIPLIER = 1
RETRY_WAIT_MIN_SECONDS = 1
RETRY_WAIT_MAX_SECONDS = 10


def is_retryable_httpx_error(exc: BaseException) -> bool:
    """Return True for transient httpx2 errors that warrant a retry.

    Retries on:
    - Connection errors (httpx2.ConnectError)
    - Timeouts (httpx2.TimeoutException)
    - Server errors (HTTP 5xx)

    Does not retry on client errors (HTTP 4xx) or other exceptions.
    """
    if isinstance(exc, (httpx2.ConnectError, httpx2.TimeoutException)):
        return True
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
    return False


retry_on_transient_error = retry(
    retry=retry_if_exception(is_retryable_httpx_error),
    wait=wait_exponential(multiplier=RETRY_WAIT_MULTIPLIER, min=RETRY_WAIT_MIN_SECONDS, max=RETRY_WAIT_MAX_SECONDS),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


DEFAULT_RETRY_WAIT: WaitBaseT = wait_exponential(
    multiplier=RETRY_WAIT_MULTIPLIER, min=RETRY_WAIT_MIN_SECONDS, max=RETRY_WAIT_MAX_SECONDS
)


def request_with_retry(
    method: str,
    url: str,
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    wait: WaitBaseT = DEFAULT_RETRY_WAIT,
    should_retry: Callable[[BaseException], bool] = is_retryable_httpx_error,
    **kwargs: Any,
) -> httpx2.Response:
    """Make an HTTP request with automatic retries on transient errors.

    Convenience wrapper for standalone/static-method use where a decorated
    closure would otherwise be needed.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, etc.).
        url: The URL to request.
        max_attempts: Maximum number of retry attempts.
        wait: Tenacity wait strategy for backoff between retries.
        should_retry: Decides whether a raised exception is retried.
        **kwargs: Passed through to httpx2.request.

    Returns:
        The httpx2.Response (already checked via raise_for_status).
    """

    @retry(
        retry=retry_if_exception(should_retry),
        wait=wait,
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _do_request() -> httpx2.Response:
        response = httpx2.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    return _do_request()
