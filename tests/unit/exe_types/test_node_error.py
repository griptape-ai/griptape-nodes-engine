# ruff: noqa: S106

from __future__ import annotations

from typing import Any

import httpx
import pytest

from griptape_nodes.exe_types.node_error import NodeError, NodeErrorLink


class FakeResponse:
    def __init__(
        self, status_code: int, body: Any = None, *, text: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        if self._body is None:
            msg = "not json"
            raise ValueError(msg)
        return self._body


class TestMissingSecret:
    def test_links_to_the_secret_and_the_key_page(self) -> None:
        error = NodeError.from_missing_secret("MY KEY", key_url="https://example.com/keys")

        assert "MY KEY is not set" in str(error)
        assert error.links[0] == NodeErrorLink(label="Add the API key", url="#settings-secrets?filter=MY%20KEY")
        assert error.links[1].url == "https://example.com/keys"

    def test_subclass_gets_its_own_type(self) -> None:
        class MineError(NodeError, ValueError):
            pass

        assert isinstance(MineError.from_missing_secret("K"), MineError)


class TestFromHttpResponse:
    def test_provider_message_fields_and_response(self) -> None:
        body = {"error": {"message": "Bad size", "code": "invalid_size"}}
        response = FakeResponse(400, body, headers={"x-request-id": "req_1"})

        error = NodeError.from_http_response(response, provider="Acme", fields={"job_id": "j1"})

        assert str(error) == "Bad size"
        assert error.fields == {"status_code": 400, "request_id": "req_1", "error_code": "invalid_size", "job_id": "j1"}
        assert error.response == body
        assert error.links == []

    def test_validation_list_becomes_one_message(self) -> None:
        body = {"detail": [{"loc": ["body", "width"], "msg": "must be a multiple of 32"}, {"msg": "prompt is empty"}]}

        error = NodeError.from_http_response(FakeResponse(422, body), provider="Acme")

        assert str(error) == "width: must be a multiple of 32; prompt is empty"

    @pytest.mark.parametrize("status", [401, 403])
    def test_refused_key_links_to_the_secret(self, status: int) -> None:
        error = NodeError.from_http_response(
            FakeResponse(status, {"detail": "nope"}),
            provider="Acme",
            secret_name="ACME_KEY",
        )

        assert str(error).startswith("Acme refused the API key. Update ACME_KEY")
        assert [link.label for link in error.links] == ["Update the API key"]

    def test_key_refused_false_treats_a_401_as_an_ordinary_error(self) -> None:
        body = {"detail": {"status": "missing_permissions", "message": "The key lacks text_to_speech."}}

        error = NodeError.from_http_response(
            FakeResponse(401, body), provider="Acme", secret_name="ACME_KEY", key_refused=False
        )

        assert str(error) == "The key lacks text_to_speech."
        assert error.fields == {"status_code": 401, "error_code": "missing_permissions"}
        assert error.links == []

    def test_refused_key_without_a_secret_has_no_link(self) -> None:
        error = NodeError.from_http_response(FakeResponse(401), provider="Acme")

        assert str(error) == "Acme refused the API key."
        assert error.links == []

    def test_malformed_key_reported_by_the_provider_links_to_the_secret(self) -> None:
        error = NodeError.from_http_response(
            FakeResponse(422, {"detail": "Invalid API key format"}), provider="Acme", secret_name="ACME_KEY"
        )

        assert str(error) == "Invalid API key format"
        assert error.links[0].label == "Update the API key"

    @pytest.mark.parametrize(
        "reason",
        ["Invalid api_key", "Bad apiKey header", "API-key format is wrong", "API keys are disabled", "invalid_api_key"],
    )
    def test_other_spellings_of_api_key_link_to_the_secret(self, reason: str) -> None:
        error = NodeError.from_http_response(
            FakeResponse(422, {"detail": reason}), provider="Acme", secret_name="ACME_KEY"
        )

        assert str(error) == reason
        assert error.links[0].label == "Update the API key"

    def test_a_word_containing_apikey_is_not_a_mention(self) -> None:
        error = NodeError.from_http_response(
            FakeResponse(422, {"detail": "Unknown field 'rapikeyword'"}), provider="Acme", secret_name="ACME_KEY"
        )

        assert error.links == []

    def test_body_that_is_not_json_is_attached_as_text(self) -> None:
        error = NodeError.from_http_response(FakeResponse(502, text="x" * 5000), provider="Acme")

        assert str(error) == "Acme had a server error (HTTP 502). Run the node again in a few minutes."
        assert error.response == {"body": "x" * 2000}

    def test_fallback_message_names_the_status(self) -> None:
        assert str(NodeError.from_http_response(FakeResponse(418), provider="Acme")) == "Acme returned HTTP 418."

    def test_success_with_unreadable_body(self) -> None:
        error = NodeError.from_http_response(FakeResponse(200, text="<html>"), provider="Acme")

        assert str(error) == "Acme sent a response the node could not read (HTTP 200)."

    def test_accepts_an_httpx_response(self) -> None:
        response = httpx.Response(429, json={"message": "Slow down"}, headers={"request-id": "r9"})

        error = NodeError.from_http_response(response, provider="Acme")

        assert str(error) == "Slow down"
        assert error.fields == {"status_code": 429, "request_id": "r9"}


class TestFromRequestException:
    def test_exception_with_a_response_uses_it(self) -> None:
        exc = httpx.HTTPStatusError("bad", request=httpx.Request("GET", "https://x"), response=httpx.Response(500))

        error = NodeError.from_request_exception(exc, provider="Acme")

        assert error.fields == {"status_code": 500}

    def test_timeout(self) -> None:
        error = NodeError.from_request_exception(httpx.ReadTimeout("slow"), provider="Acme")

        assert str(error) == "Acme did not answer in time. Run the node again."

    def test_connection_failure(self) -> None:
        error = NodeError.from_request_exception(httpx.ConnectError("down"), provider="Acme")

        assert str(error).startswith("Could not reach Acme.")

    def test_unknown_exception_keeps_its_text(self) -> None:
        error = NodeError.from_request_exception(ValueError("odd"), provider="Acme")

        assert str(error) == "The request to Acme failed: odd"


class TestStreamedHttpxResponse:
    def test_unread_stream_gives_status_only_fields(self) -> None:
        with (
            httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(401, content=iter([b"x"])))) as client,
            client.stream("GET", "https://x") as response,
        ):
            error = NodeError.from_http_response(response, provider="Acme")

        assert str(error) == "Acme refused the API key."
        assert error.fields == {"status_code": 401}
        assert error.response is None


class TestRequestExceptionKeyRefused:
    def test_key_refused_false_is_forwarded(self) -> None:
        exc = httpx.HTTPStatusError(
            "bad",
            request=httpx.Request("GET", "https://x"),
            response=httpx.Response(401, json={"message": "No permission"}),
        )

        error = NodeError.from_request_exception(exc, provider="Acme", secret_name="ACME_KEY", key_refused=False)

        assert str(error) == "No permission"
        assert error.links == []
