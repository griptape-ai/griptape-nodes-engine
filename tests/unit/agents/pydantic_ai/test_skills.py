"""Tests for how the runner wires `.agents/skills` into a `SkillsCapability`.

Skill discovery and progressive disclosure are owned by `pydantic-ai-skills`;
these tests cover only the runner's contract: when a capability is built, what
it discovers, and which tools it exposes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.agents.pydantic_ai.runner import PydanticAgentRunner
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver

if TYPE_CHECKING:
    from pathlib import Path


def _runner(workspace: Path, threads_dir: Path, *, auto_load_skills: bool = True) -> PydanticAgentRunner:
    """Build a runner rooted at `workspace` without touching Griptape Cloud."""
    storage = LocalThreadStorageDriver(threads_dir, config_manager=None, secrets_manager=None)  # type: ignore[arg-type]
    return PydanticAgentRunner(
        model_name="test",
        api_key="dummy",
        workspace_root=workspace,
        storage=storage,
        auto_load_skills=auto_load_skills,
    )


def _write_skill(workspace: Path, name: str, body: str = "Guidance for the task.") -> None:
    """Create a minimal valid `.agents/skills/<name>/SKILL.md` under `workspace`."""
    skill_dir = workspace / ".agents/skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace root."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_no_capability_when_skills_dir_missing(workspace: Path, tmp_path: Path) -> None:
    """A workspace with no `.agents/skills` dir yields no skills capability."""
    runner = _runner(workspace, tmp_path / "threads")
    assert runner._build_skills_capabilities() == []


def test_no_capability_when_disabled(workspace: Path, tmp_path: Path) -> None:
    """`auto_load_skills=False` suppresses the capability even when skills exist."""
    _write_skill(workspace, "demo-skill")
    runner = _runner(workspace, tmp_path / "threads", auto_load_skills=False)
    assert runner._build_skills_capabilities() == []


def test_discovers_skill_and_excludes_script_tool(workspace: Path, tmp_path: Path) -> None:
    """A present skill is discovered; `run_skill_script` is excluded from the tools."""
    _write_skill(workspace, "demo-skill")
    runner = _runner(workspace, tmp_path / "threads")

    capabilities = runner._build_skills_capabilities()
    assert len(capabilities) == 1

    toolset = capabilities[0].toolset
    assert "demo-skill" in toolset.skills
    assert "run_skill_script" not in toolset.tools
    assert {"list_skills", "load_skill"} <= set(toolset.tools)
