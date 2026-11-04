"""The exception a node raises to send its failure to the editor in parts.

Node libraries import ``NodeError`` and ``NodeErrorLink`` from ``griptape_nodes.exe_types.core_types``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

# Class names rather than types, so this works for requests and httpx without importing either.
_TIMEOUT_CLASS_NAMES = frozenset({"Timeout", "TimeoutException", "TimeoutError"})
_CONNECTION_CLASS_NAMES = frozenset({"ConnectionError", "ConnectError", "NetworkError"})

_KEY_REFUSED_STATUSES = (401, 403)
_HTTP_ERROR_STATUS = 400
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_STATUS = 500
_MAX_BODY_TEXT_CHARS = 2000
# "API key", "api_key", "api-key" or "apikey", singular or plural, in a provider's message, such as
# a 422 with "Invalid API key format" or the code "invalid_api_key". Only a letter next to it rules a
# match out, because "_" and digits often join the words in error codes.
_API_KEY_MENTION = re.compile(r"(?<![a-z])api[ _-]?keys?(?![a-z])", re.IGNORECASE)

# Response headers providers use for the ID of a request, in the order they are checked.
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-trace-id", "x-amzn-requestid", "cf-ray")


@dataclass
class NodeErrorLink:
    label: str
    """Link text, e.g. "Supported image formats"."""

    url: str
    """An http or https page, or a place in the editor starting with "#", such as
    "#settings-secrets?filter=MY_KEY". URL-encode any values in a "#" link's query.

    The engine accepts any "#" link and passes it through. The editor decides which places it
    opens, and only follows routes that navigate, never ones that change anything."""

    @classmethod
    def from_secret(cls, secret_name: str, label: str = "Add the API key") -> NodeErrorLink:
        """A link that opens Settings → API Keys & Secrets filtered to one secret."""
        return cls(label=label, url=f"#settings-secrets?filter={quote(secret_name)}")


class NodeError(Exception):
    """A node failure with parts the editor shows on their own lines.

    Raise it from a node instead of building those parts into the message:

        raise NodeError(
            "Processing failed: proxy client error",
            fields={"generation_id": generation_id},
            response=response_json,
        )

    Args:
        message: What went wrong, for an artist. Leave out the node's name; the editor shows it.
        fields: Labelled values a user may need to quote to support, such as a request ID.
            Values are shown as text.
        response: The provider's response body. Never include headers. Dropped if it is not
            JSON-serializable. If it is larger than 16 KB, long strings are cut down first, and
            it is dropped only if it is still too large.
        links: Up to three http(s) pages that explain the failure, or "#" links that open the
            place in the editor where the user can fix it.
    """

    def __init__(
        self,
        message: str,
        *,
        fields: dict[str, str | int | float | bool] | None = None,
        response: dict[str, Any] | None = None,
        links: list[NodeErrorLink] | None = None,
    ) -> None:
        super().__init__(message)
        self.fields = fields or {}
        self.response = response
        self.links = links or []

    @classmethod
    def from_missing_secret(cls, secret_name: str, *, key_url: str | None = None) -> NodeError:
        """The error for a secret that is not set, linking to it in Settings.

        Raise it from `validate_before_node_run` so the node fails before it starts.

        Args:
            secret_name: The secret, e.g. "OPENAI_API_KEY".
            key_url: The provider's page for getting a key, added as a second link.
        """
        links = [NodeErrorLink.from_secret(secret_name)]
        if key_url:
            links.append(NodeErrorLink(label="Get an API key", url=key_url))
        return cls(
            f"{secret_name} is not set. Add it in Settings → API Keys & Secrets, then run the node again.",
            links=links,
        )

    @classmethod
    def from_http_response(
        cls,
        response: Any,
        *,
        provider: str,
        secret_name: str | None = None,
        key_refused: bool | None = None,
        fields: dict[str, str | int | float | bool] | None = None,
    ) -> NodeError:
        """The error for a provider's HTTP response that the node can't use.

        Takes a `requests` or `httpx` response. Builds all of the parts:

        - message: the provider's own explanation when the body has one, else a plain sentence
          for the status. A 401 or 403 says the key was refused.
        - fields: `status_code`, plus `request_id` and `error_code` where the response has them.
        - response: the JSON body, or the start of the text when the body is not JSON.
        - links: for a refused key, "Update the API key", when `secret_name` is given.

        Args:
            response: Anything with `status_code`, `headers`, `json()` and `text`.
            provider: The provider's name as an artist knows it, e.g. "Black Forest Labs".
            secret_name: The API key secret. Without it a refused key gets no link.
            key_refused: Whether the provider refused the API key. Leave it as None to treat a 401
                or 403 as a refused key. Pass False for a provider that also uses those statuses
                for other problems, such as a key that lacks a permission.
            fields: Extra values for `fields`, such as a job ID. These win over the ones read from
                the response.
        """
        status = response.status_code
        body = _json_object(response)
        reason = _provider_reason(body)
        if key_refused is None:
            key_refused = status in _KEY_REFUSED_STATUSES or _mentions_api_key(reason)

        links: list[NodeErrorLink] = []
        if key_refused and secret_name:
            links.append(NodeErrorLink.from_secret(secret_name, "Update the API key"))

        message = _status_message(provider, status, reason, key_refused=key_refused, secret_name=secret_name)
        all_fields = _response_fields(response, body)
        all_fields.update(fields or {})
        return cls(message, fields=all_fields, response=_response_body(response, body), links=links)

    @classmethod
    def from_request_exception(
        cls,
        exc: Exception,
        *,
        provider: str,
        secret_name: str | None = None,
        key_refused: bool | None = None,
        fields: dict[str, str | int | float | bool] | None = None,
    ) -> NodeError:
        """The error for a failed call to a provider, from a `requests` or `httpx` exception.

        Use it so users see "Could not reach X" and not `HTTPSConnectionPool(...): Read timed out`:

            try:
                response = requests.post(url, json=payload, timeout=60)
                response.raise_for_status()
            except requests.RequestException as e:
                raise NodeError.from_request_exception(e, provider="Acme", secret_name="ACME_API_KEY") from e

        An exception that carries a response goes through :meth:`from_http_response`. A timeout and
        a failed connection each get their own message. Anything else gets the exception's text.
        The arguments are those of :meth:`from_http_response`.
        """
        response = getattr(exc, "response", None)
        if response is not None:
            return cls.from_http_response(
                response, provider=provider, secret_name=secret_name, key_refused=key_refused, fields=fields
            )

        class_names = {klass.__name__ for klass in type(exc).__mro__}
        if class_names & _TIMEOUT_CLASS_NAMES:
            message = f"{provider} did not answer in time. Run the node again."
        elif class_names & _CONNECTION_CLASS_NAMES:
            message = f"Could not reach {provider}. Check your internet connection and run the node again."
        else:
            message = f"The request to {provider} failed: {exc}"
        return cls(message, fields=fields)


def _mentions_api_key(reason: str | None) -> bool:
    if reason is None:
        return False
    return _API_KEY_MENTION.search(reason) is not None


def _json_object(response: Any) -> dict[str, Any] | None:
    try:
        body = response.json()
    # RuntimeError for an httpx stream whose body was never read.
    except (ValueError, RuntimeError):
        return None
    if isinstance(body, dict) and body:
        return body
    return None


def _response_body(response: Any, body: dict[str, Any] | None) -> dict[str, Any] | None:
    if body is not None:
        return body
    try:
        text = response.text
    except RuntimeError:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return {"body": text[:_MAX_BODY_TEXT_CHARS]}


def _response_fields(response: Any, body: dict[str, Any] | None) -> dict[str, str | int | float | bool]:
    fields: dict[str, str | int | float | bool] = {"status_code": response.status_code}
    request_id = _request_id(response.headers, body)
    if request_id:
        fields["request_id"] = request_id
    error = _error_object(body)
    # Some providers name the error in the error object's `status`, such as "voice_not_found".
    error_code = _first_string(error, body, keys=("code", "errorCode", "error_code"))
    if error_code is None:
        error_code = _first_string(error, keys=("status",))
    if error_code:
        fields["error_code"] = error_code
    return fields


def _request_id(headers: Any, body: dict[str, Any] | None) -> str | None:
    """The request ID in the body, which names the failed request, else one in the headers."""
    from_body = _first_string(_error_object(body), body, keys=("request_id", "requestId"))
    if from_body:
        return from_body
    if headers is None:
        return None
    for name in _REQUEST_ID_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _first_string(*sources: dict[str, Any] | None, keys: tuple[str, ...]) -> str | None:
    for source in sources:
        if source is None:
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _error_object(body: dict[str, Any] | None) -> dict[str, Any] | None:
    """The object holding the error's details: `body["error"]` or `body["detail"]` when it is one."""
    if body is None:
        return None
    for key in ("error", "detail"):
        value = body.get(key)
        if isinstance(value, dict):
            return value
    return None


def _provider_reason(body: dict[str, Any] | None) -> str | None:
    """The provider's own explanation: a message, a string `error` or `detail`, or a list of problems."""
    if body is None:
        return None
    error = _error_object(body)
    if error is not None:
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    for key in ("detail", "error", "message"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    detail = body.get("detail")
    if isinstance(detail, list):
        problems = [_validation_problem(item) for item in detail if isinstance(item, dict)]
        return "; ".join(problem for problem in problems if problem) or None
    return None


def _validation_problem(item: dict[str, Any]) -> str | None:
    """One entry of a validation `detail` list, such as `width: Input should be a multiple of 32`."""
    message = item.get("msg")
    if not isinstance(message, str) or not message.strip():
        return None
    location = item.get("loc")
    if isinstance(location, list) and location and isinstance(location[-1], str):
        return f"{location[-1]}: {message.strip()}"
    return message.strip()


def _status_message(
    provider: str, status: int, reason: str | None, *, key_refused: bool, secret_name: str | None
) -> str:
    if key_refused and (status in _KEY_REFUSED_STATUSES or reason is None):
        return _key_refused_message(provider, secret_name)
    if reason:
        return reason
    return _fallback_message(provider, status)


def _key_refused_message(provider: str, secret_name: str | None) -> str:
    if secret_name:
        return f"{provider} refused the API key. Update {secret_name} in Settings → API Keys & Secrets, then run the node again."
    return f"{provider} refused the API key."


def _fallback_message(provider: str, status: int) -> str:
    if status < _HTTP_ERROR_STATUS:
        return f"{provider} sent a response the node could not read (HTTP {status})."
    if status == _HTTP_TOO_MANY_REQUESTS:
        return f"{provider} is rate-limiting requests. Wait a moment and run the node again."
    if status >= _HTTP_SERVER_ERROR_STATUS:
        return f"{provider} had a server error (HTTP {status}). Run the node again in a few minutes."
    return f"{provider} returned HTTP {status}."
