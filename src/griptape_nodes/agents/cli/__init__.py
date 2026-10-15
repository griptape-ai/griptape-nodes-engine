"""Coding-agent CLI harnesses."""

from griptape_nodes.agents.cli.base import CliHarness, CliHarnessError
from griptape_nodes.agents.cli.claude_code import ClaudeCodeHarness

CLI_HARNESSES: dict[str, type[CliHarness]] = {harness.harness_id: harness for harness in (ClaudeCodeHarness,)}

__all__ = ["CLI_HARNESSES", "CliHarness", "CliHarnessError"]
