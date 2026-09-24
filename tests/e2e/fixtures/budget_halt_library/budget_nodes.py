"""Fixture nodes for the budget-halt e2e tests.

``CloudCallNode`` is a ``SuccessFailureNode`` that makes one HTTP call to a URL the test supplies
and hands whatever it raises to the same two functions a real credit-spending node uses:
``refusal_from_exception`` to decide whether Griptape Cloud refused it over budget, and
``_handle_failure_exception`` to route the result. Pointed at a stub that answers 403 with a
recorded refusal body, it exercises the whole path -- ``httpx`` raising, the chain being walked,
the body being parsed, the halt being worded, and the Failed branch being overridden -- with no
network and no Cloud account.

Two instances of the one node type make the test: the first is refused, the second hangs off its
Failed output and writes a file if it ever runs. That file is how a run that routed down Failed
past a budget block becomes visible.

Kept to ``httpx``, which the engine already requires, so the library registers cleanly in an
isolated engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from griptape_nodes.drivers.cloud_credentials import resolve_cloud_host
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import SuccessFailureNode
from griptape_nodes.utils.budget_refusal import BudgetExceededError, refusal_from_exception
from griptape_nodes.utils.budget_refusal import describe as describe_budget_refusal

_REQUEST_TIMEOUT_SECONDS = 10.0


class CloudCallNode(SuccessFailureNode):
    """Calls the URL it is given and routes the result the way a credit-spending node does."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="url",
                tooltip="Endpoint to call. The test points this at a stub standing in for Griptape Cloud.",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="receipt_file",
                tooltip="Path this node writes to as soon as it runs, so a test can see that it did.",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip="The response body, when the call went through",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )
        self._create_status_parameters()

    def process(self) -> None:
        self._clear_execution_status()
        self._record_that_it_ran()

        url = self.get_parameter_value("url") or ""
        try:
            response = httpx.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self._fail(exc)
            return

        self.parameter_output_values["result"] = response.text
        self._set_status_results(was_successful=True, result_details="The call went through.")

    def _record_that_it_ran(self) -> None:
        receipt_path = self.get_parameter_value("receipt_file") or ""
        if not receipt_path:
            return
        receipt_file = Path(receipt_path)
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        receipt_file.write_text(self.name)

    def _fail(self, exc: httpx.HTTPError) -> None:
        """Turn a failed call into either a budget halt or an ordinary node failure.

        The order matters and is the same order the standard library's proxy node uses: ask
        whether Cloud refused this over budget first, because that answer changes what the
        failure *is*, not just how it is worded.
        """
        cloud_host = resolve_cloud_host(self.engine.secrets_manager)
        refusal = refusal_from_exception(exc, cloud_host=cloud_host)
        if refusal is not None:
            halt = describe_budget_refusal(refusal, node_name=self.name)
            self._set_status_results(was_successful=False, result_details=halt)
            self._handle_failure_exception(BudgetExceededError(halt, refusal))
            return

        details = f"The call failed: {exc}"
        self._set_status_results(was_successful=False, result_details=details)
        self._handle_failure_exception(RuntimeError(details))
