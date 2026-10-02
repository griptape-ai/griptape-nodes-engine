"""Contract tests for ``sanitize_attachments``: what a malformed ``NodeError`` attachment becomes.

A node library can pass anything as ``fields``, ``response``, or ``links``, and a worker can send a
damaged payload. Each malformed part is dropped on its own, so the rest of the error still reaches
the editor and reporting the failure never raises.
"""

from typing import Any

from griptape_nodes.exe_types.node_error import NodeErrorLink
from griptape_nodes.retained_mode.events.node_error_details import RESPONSE_DROPPED_FIELD, sanitize_attachments

DOCS_LINK = NodeErrorLink(label="Docs", url="https://docs.griptapenodes.com/")


def _sanitize(*, fields: Any = None, response: Any = None, links: Any = None) -> Any:
    return sanitize_attachments(fields, response, links)


class TestFields:
    def test_missing_fields_are_empty(self) -> None:
        assert _sanitize(fields=None).fields == {}

    def test_fields_that_are_not_a_dict_are_dropped(self) -> None:
        assert _sanitize(fields=[("request_id", "r1")]).fields == {}

    def test_only_the_malformed_entries_are_dropped(self) -> None:
        fields = {"request_id": "r1", 42: "not a string key", "details": {"nested": "dict"}, "retries": 3}

        assert _sanitize(fields=fields).fields == {"request_id": "r1", "retries": "3"}


class TestResponse:
    def test_missing_response_is_none_without_a_marker(self) -> None:
        attachments = _sanitize(response=None)

        assert attachments.response is None
        assert attachments.fields == {}

    def test_response_that_is_not_a_dict_is_dropped_without_a_marker(self) -> None:
        # The marker says a response was too big or unreadable. A list or a string was never a
        # response body in the first place, so there is nothing to tell the user about.
        for response in (["status", "ERRORED"], "status: ERRORED"):
            attachments = _sanitize(response=response)

            assert attachments.response is None
            assert RESPONSE_DROPPED_FIELD not in attachments.fields


class TestLinks:
    def test_missing_links_are_empty(self) -> None:
        assert _sanitize(links=None).links == []

    def test_links_that_are_not_a_list_are_dropped(self) -> None:
        assert _sanitize(links="https://docs.griptapenodes.com/").links == []

    def test_a_tuple_of_links_is_accepted(self) -> None:
        assert _sanitize(links=(DOCS_LINK,)).links == [DOCS_LINK]

    def test_the_dict_form_a_worker_sends_is_accepted(self) -> None:
        links = [{"label": "Docs", "url": "https://docs.griptapenodes.com/"}]

        assert _sanitize(links=links).links == [DOCS_LINK]

    def test_a_link_of_the_wrong_type_is_dropped_and_the_rest_kept(self) -> None:
        links = ["https://docs.griptapenodes.com/", DOCS_LINK]

        assert _sanitize(links=links).links == [DOCS_LINK]

    def test_a_link_with_a_non_string_label_or_url_is_dropped(self) -> None:
        links = [
            {"label": None, "url": "https://docs.griptapenodes.com/"},
            {"label": "Docs", "url": 42},
            {"label": "Docs"},
            DOCS_LINK,
        ]

        assert _sanitize(links=links).links == [DOCS_LINK]
