from __future__ import annotations

import pytest

from griptape_nodes.retained_mode.topics import (
    client_response_topic,
    default_response_topic,
    engine_id_of_response_topic,
    engine_response_topic,
)


class TestEngineIdOfResponseTopic:
    def test_inverts_engine_response_topic(self) -> None:
        assert engine_id_of_response_topic(engine_response_topic("worker-1")) == "worker-1"

    @pytest.mark.parametrize(
        "topic",
        ["engines/worker-1/request", "sessions/abc/response", "engines//response", "response"],
    )
    def test_is_none_for_other_topics(self, topic: str) -> None:
        assert engine_id_of_response_topic(topic) is None


class TestClientResponseTopic:
    def test_is_under_the_session_when_there_is_one(self) -> None:
        assert client_response_topic("s1", "c1") == "sessions/s1/clients/c1/response"

    def test_stands_alone_with_no_session(self) -> None:
        assert client_response_topic(None, "c1") == "clients/c1/response"


class TestDefaultResponseTopic:
    def test_is_the_session_topic_when_there_is_a_session(self) -> None:
        assert default_response_topic("e1", "s1") == "sessions/s1/response"

    def test_is_the_engine_topic_with_no_session(self) -> None:
        assert default_response_topic("e1", None) == "engines/e1/response"
