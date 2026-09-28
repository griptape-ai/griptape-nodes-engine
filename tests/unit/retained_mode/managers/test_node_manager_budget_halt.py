"""A node whose Griptape Cloud call was refused over budget fails as a budget halt naming that node.

Every node failure passes through ``NodeManager._execution_failure``, so no node type has to
recognize the refusal itself. These drive it with the three shapes a refusal arrives in: the raw
Cloud 403, a halt a Cloud driver raised before it knew which node it was running in, and a halt
already worded for its node.
"""

import logging
from unittest.mock import MagicMock

import httpx
import pytest

from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.utils.budget_refusal import (
    BUDGET_HALT_PREFIX,
    BudgetExceededError,
    describe,
    refusal_from_body,
)
from tests.unit.utils.test_budget_refusal import CLOUD_HOST, a_refusal_body


def _node_manager() -> NodeManager:
    """A NodeManager whose engine points at the Cloud deployment the fixtures come from."""
    engine = MagicMock()
    engine.secrets_manager.get_secret.return_value = f"https://{CLOUD_HOST}"
    return NodeManager(MagicMock(), engine=engine)


def _cloud_refusal() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"https://{CLOUD_HOST}/api/proxy/v2/models/topaz")
    response = httpx.Response(403, json=a_refusal_body(), request=request)
    return httpx.HTTPStatusError("403 Forbidden", request=request, response=response)


def _driver_halt() -> BudgetExceededError:
    """A halt raised below the node, which does not know the node's name."""
    refusal = refusal_from_body(a_refusal_body())
    assert refusal is not None
    return BudgetExceededError(describe(refusal), refusal)


class TestExecutionFailureNamesTheRefusedNode:
    def test_a_raw_cloud_refusal_becomes_a_halt(self, caplog: pytest.LogCaptureFixture) -> None:
        wrapped = RuntimeError("Attempted to upscale the video. Failed due to a 403.")
        wrapped.__cause__ = _cloud_refusal()

        with caplog.at_level(logging.ERROR, logger="griptape_nodes"):
            failure = _node_manager()._execution_failure(wrapped, "Upscale")

        assert isinstance(failure.exception, BudgetExceededError)
        assert failure.exception.node_name == "Upscale"
        assert str(failure.result_details).startswith(BUDGET_HALT_PREFIX)
        assert "'Upscale'" in str(failure.result_details)
        assert '"tight"' in str(failure.result_details)
        assert any("Upscale" in record.getMessage() for record in caplog.records)

    def test_a_driver_halt_is_reworded_to_name_the_node(self) -> None:
        driver_halt = _driver_halt()
        wrapped = RuntimeError("the driver gave up")
        wrapped.__cause__ = driver_halt

        failure = _node_manager()._execution_failure(wrapped, "Upscale")

        assert isinstance(failure.exception, BudgetExceededError)
        assert failure.exception.node_name == "Upscale"
        assert failure.exception.refusal is driver_halt.refusal
        assert "'Upscale'" in str(failure.result_details)

    def test_a_halt_that_already_names_its_node_is_kept(self) -> None:
        """A nested node's halt reaches its parent wrapped, and must still name the node refused."""
        refusal = refusal_from_body(a_refusal_body())
        assert refusal is not None
        inner = BudgetExceededError(describe(refusal, node_name="Inner"), refusal, node_name="Inner")
        outer = RuntimeError("the subflow failed")
        outer.__cause__ = inner

        failure = _node_manager()._execution_failure(outer, "Group")

        assert failure.exception is inner
        assert str(failure.result_details) == str(inner)

    def test_an_ordinary_failure_is_reported_as_before(self) -> None:
        error = ValueError("the image was empty")

        failure = _node_manager()._execution_failure(error, "Blur")

        assert failure.exception is error
        assert str(failure.result_details) == "Attempted to execute node 'Blur'. Failed with error: the image was empty"
