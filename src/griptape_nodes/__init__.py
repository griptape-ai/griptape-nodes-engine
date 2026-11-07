"""Griptape Nodes package."""

from griptape_nodes.appended_python_path import append_python_path

# Every engine process imports this package first: the CLI, library workers, and saved workflows run
# as scripts. Applying the paths here covers all of them before any node code imports.
append_python_path()
