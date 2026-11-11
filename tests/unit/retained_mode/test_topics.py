from __future__ import annotations

import pytest

from griptape_nodes.retained_mode.topics import (
    audience_topic,
    client_response_topic,
    engine_id_of_response_topic,
    engine_response_topic,
    worker_engine_id_of_topic,
    workflow_events_topic,
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


class TestWorkflowEventsTopic:
    def test_is_under_the_session_when_there_is_one(self) -> None:
        assert workflow_events_topic("e1", "s1", "wf-1") == "sessions/s1/workflows/wf-1/events"

    def test_is_under_the_engine_with_no_session(self) -> None:
        assert workflow_events_topic("e1", None, "wf-1") == "engines/e1/workflows/wf-1/events"

    def test_is_read_as_no_engine_and_no_worker(self) -> None:
        topic = workflow_events_topic("e1", "s1", "wf-1")

        assert engine_id_of_response_topic(topic) is None
        assert worker_engine_id_of_topic(topic) is None


class TestAudienceTopic:
    def test_is_the_workflow_topic_for_a_workflow(self) -> None:
        assert audience_topic("e1", "s1", "wf-1") == "sessions/s1/workflows/wf-1/events"

    def test_is_the_session_topic_for_no_workflow(self) -> None:
        assert audience_topic("e1", "s1", None) == "sessions/s1/response"

    def test_is_the_engine_topic_for_no_workflow_and_no_session(self) -> None:
        assert audience_topic("e1", None, None) == "engines/e1/response"
