"""Entry point for a library's worker process: ``python -m griptape_nodes.worker [--library-name NAME]...``.

Started by the engine it works for, which hands it the channel to connect to. Needs only the
griptape-nodes-engine package and the library's own dependencies.
"""

from __future__ import annotations

import argparse
import sys

from griptape_nodes.worker.runtime import run_worker


def main(argv: list[str] | None = None) -> int:
    """Run the worker with `argv`'s options, returning its exit code."""
    parser = argparse.ArgumentParser(prog="python -m griptape_nodes.worker", description=__doc__)
    parser.add_argument(
        "--library-name",
        dest="library_names",
        action="append",
        default=[],
        help="A library this worker runs the nodes of; repeat it for more. Without it, the worker loads every library.",
    )
    args = parser.parse_args(argv)
    return run_worker(args.library_names)


if __name__ == "__main__":
    sys.exit(main())
