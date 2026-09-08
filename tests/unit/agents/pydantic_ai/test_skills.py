"""Tests for how the runner wires `.agents/skills` into a `SkillsCapability`.

Skill discovery and progressive disclosure are owned by `pydantic-ai-skills`;
these tests cover only the runner's contract: when a capability is built, what
it discovers, which tools it exposes, and what a broken skill costs.

The skills reaching the model on a run is covered in `test_runner.py`, which is
where a `FunctionModel` can observe the offered tools.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.toolsets import FunctionToolset

from griptape_nodes.agents.pydantic_ai.runner import PydanticAgentRunner
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver


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


def _write_skill(workspace: Path, name: str, body: str = "Guidance for the task.") -> Path:
    """Create a minimal valid `.agents/skills/<name>/SKILL.md` under `workspace`."""
    skill_dir = workspace / ".agents/skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}")
    return skill_dir


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


def test_discovers_skill(workspace: Path, tmp_path: Path) -> None:
    """A present skill is discovered and exposed to the model by name."""
    _write_skill(workspace, "demo-skill")
    runner = _runner(workspace, tmp_path / "threads")

    capabilities = runner._build_skills_capabilities()
    assert len(capabilities) == 1
    assert capabilities[0].skill_names == ["demo-skill"]


def test_excludes_script_tool(workspace: Path, tmp_path: Path) -> None:
    """A skill shipping both kinds of files gets the resource tool but not the script tool."""
    skill_dir = _write_skill(workspace, "demo-skill")
    (skill_dir / "references").mkdir()
    (skill_dir / "references/notes.md").write_text("Reference material.")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts/go.py").write_text("print('hi')\n")
    runner = _runner(workspace, tmp_path / "threads")

    toolset = runner._build_skills_capabilities()[0].get_toolset()

    assert isinstance(toolset, FunctionToolset)
    assert "read_skill_resource" in toolset.tools
    assert "run_skill_script" not in toolset.tools


def test_broken_skill_costs_only_itself(workspace: Path, tmp_path: Path) -> None:
    """A `SKILL.md` the loader rejects is skipped; the valid skills beside it still load."""
    _write_skill(workspace, "good-skill")
    broken = workspace / ".agents/skills/broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("no frontmatter at all\n")
    runner = _runner(workspace, tmp_path / "threads")

    capabilities = runner._build_skills_capabilities()

    assert len(capabilities) == 1
    assert capabilities[0].skill_names == ["good-skill"]


def test_unreadable_skill_costs_skills_not_the_run(workspace: Path, tmp_path: Path) -> None:
    """A `SKILL.md` that cannot be read yields no capability rather than raising."""
    skill_dir = _write_skill(workspace, "demo-skill")
    runner = _runner(workspace, tmp_path / "threads")
    with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
        assert runner._build_skills_capabilities() == []
    assert (skill_dir / "SKILL.md").is_file()


def test_no_capability_when_every_skill_is_broken(workspace: Path, tmp_path: Path) -> None:
    """A library where nothing loads yields no capability rather than an empty one."""
    broken = workspace / ".agents/skills/broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("no frontmatter at all\n")
    runner = _runner(workspace, tmp_path / "threads")

    assert runner._build_skills_capabilities() == []


def test_rebuilds_snapshot_per_call(workspace: Path, tmp_path: Path) -> None:
    """Discovery is a snapshot, so a skill added after the first build still lands."""
    _write_skill(workspace, "first-skill")
    runner = _runner(workspace, tmp_path / "threads")
    assert runner._build_skills_capabilities()[0].skill_names == ["first-skill"]

    _write_skill(workspace, "second-skill")

    assert runner._build_skills_capabilities()[0].skill_names == ["first-skill", "second-skill"]
