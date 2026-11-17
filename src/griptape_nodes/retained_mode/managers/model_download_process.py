"""Child process that `ModelManager` runs to download a model.

Run as `python -m griptape_nodes.retained_mode.managers.model_download_process MODEL_ID`.
Progress goes to stdout as JSON, and a failure verdict goes to stderr. See
`griptape_nodes.utils.model_download_errors` for the wire format.
"""

import argparse
import sys

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.utils.exception_utils import readable_exception_message
from griptape_nodes.utils.model_download_errors import DownloadFailure, classify, format_error_event


def main() -> None:
    """Download the model named on the command line, exiting 1 with a verdict on failure."""
    parser = argparse.ArgumentParser(description="Download a model from Hugging Face Hub.")
    parser.add_argument("model_id")
    parser.add_argument("--local-dir", default=None)
    parser.add_argument("--revision", default="main")
    args = parser.parse_args()

    try:
        GriptapeNodes.ModelManager().download_model(
            model_id=args.model_id,
            local_dir=args.local_dir,
            revision=args.revision,
            allow_patterns=None,
            ignore_patterns=None,
        )
    except Exception as e:
        failure = DownloadFailure(kind=classify(e), detail=readable_exception_message(e))
        sys.stderr.write(format_error_event(failure))
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
