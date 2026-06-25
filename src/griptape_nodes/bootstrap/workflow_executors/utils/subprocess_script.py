"""Subprocess script to execute a Griptape Nodes workflow.

This script is intended to be run as a subprocess by the SubprocessWorkflowExecutor.
"""

import json
from argparse import ArgumentParser

from workflow import execute_workflow  # type: ignore[attr-defined]

from griptape_nodes.bootstrap.workflow_executors.local_session_workflow_executor import LocalSessionWorkflowExecutor
from griptape_nodes.utils import install_file_url_support

# Install file:// URL support for httpx/requests in subprocess
install_file_url_support()


def _main() -> None:
    parser = ArgumentParser()
    LocalSessionWorkflowExecutor.add_cli_arguments(parser)
    parser.add_argument(
        "--json-input",
        default=json.dumps({}),
        help="JSON string representing the flow input",
    )
    parser.add_argument(
        "--workflow-path",
        default=None,
        help="Path to the Griptape Nodes workflow file",
    )
    args = parser.parse_args()
    flow_input = json.loads(args.json_input)

    local_session_workflow_executor = LocalSessionWorkflowExecutor.from_cli_args(args)

    execute_workflow(
        input=flow_input,
        workflow_executor=local_session_workflow_executor,
        pickle_control_flow_result=args.pickle_control_flow_result,
    )


if __name__ == "__main__":
    _main()
