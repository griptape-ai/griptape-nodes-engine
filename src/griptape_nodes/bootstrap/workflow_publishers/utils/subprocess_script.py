import asyncio
import logging
from argparse import ArgumentParser
from dataclasses import dataclass

from griptape_nodes.bootstrap.workflow_publishers.local_session_workflow_publisher import (
    LocalSessionWorkflowPublisher,
)
from griptape_nodes.bootstrap.workflow_publishers.local_workflow_publisher import LocalWorkflowPublisher

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


@dataclass
class PublishWorkflowArgs:
    """Arguments for publishing a workflow."""

    workflow_name: str
    workflow_path: str
    publisher_name: str
    published_workflow_file_name: str
    pickle_control_flow_result: bool
    session_id: str | None = None


async def _main(args: PublishWorkflowArgs) -> None:
    publisher: LocalWorkflowPublisher
    if args.session_id is not None:
        publisher = LocalSessionWorkflowPublisher(session_id=args.session_id)
    else:
        publisher = LocalWorkflowPublisher()

    async with publisher:
        await publisher.arun(
            workflow_name=args.workflow_name,
            workflow_path=args.workflow_path,
            publisher_name=args.publisher_name,
            published_workflow_file_name=args.published_workflow_file_name,
            pickle_control_flow_result=args.pickle_control_flow_result,
        )

    msg = f"Published workflow to file: {args.published_workflow_file_name}"
    logger.info(msg)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "--workflow-name",
        help="Name of the workflow to publish",
        required=True,
    )
    parser.add_argument(
        "--workflow-path",
        help="Path to the workflow file to publish",
        required=True,
    )
    parser.add_argument(
        "--publisher-name",
        help="Name of the publisher to use",
        required=True,
    )
    parser.add_argument(
        "--published-workflow-file-name", help="Name to use for the published workflow file", required=True
    )
    parser.add_argument(
        "--pickle-control-flow-result",
        action="store_true",
        default=False,
        help="Whether to pickle control flow results",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Session ID for WebSocket event emission",
    )
    parsed_args = parser.parse_args()

    publish_args = PublishWorkflowArgs(
        workflow_name=parsed_args.workflow_name,
        workflow_path=parsed_args.workflow_path,
        publisher_name=parsed_args.publisher_name,
        published_workflow_file_name=parsed_args.published_workflow_file_name,
        pickle_control_flow_result=parsed_args.pickle_control_flow_result,
        session_id=parsed_args.session_id,
    )
    asyncio.run(_main(publish_args))
