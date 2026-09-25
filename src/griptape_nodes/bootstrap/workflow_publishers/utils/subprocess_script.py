import asyncio
import logging
from argparse import ArgumentParser
from dataclasses import dataclass

from griptape_nodes.bootstrap.workflow_publishers.local_session_workflow_publisher import (
    LocalSessionWorkflowPublisher,
)
from griptape_nodes.bootstrap.workflow_publishers.local_workflow_publisher import LocalWorkflowPublisher
from griptape_nodes.utils import install_file_url_support

# Install file:// URL support for httpx/requests in subprocess
install_file_url_support()

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


@dataclass
class PublishWorkflowArgs:
    """Arguments for publishing a workflow."""

    workflow_name: str
    workflow_path: str
    publisher_name: str
    published_workflow_file_name: str
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
        session_id=parsed_args.session_id,
    )
    asyncio.run(_main(publish_args))
