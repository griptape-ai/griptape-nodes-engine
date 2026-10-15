"""Coding-agent CLI harnesses."""

from griptape_nodes.agents.cli.base import CliHarness, CliHarnessError
from griptape_nodes.agents.cli.claude_code import ClaudeCodeHarness
from griptape_nodes.agents.cli.pi import PiHarness

CLI_HARNESSES: dict[str, type[CliHarness]] = {harness.harness_id: harness for harness in (ClaudeCodeHarness, PiHarness)}

__all__ = ["CLI_HARNESSES", "CliHarness", "CliHarnessError"]
