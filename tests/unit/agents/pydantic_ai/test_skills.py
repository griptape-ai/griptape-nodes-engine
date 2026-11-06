"""Tests for the skills the runner offers the model from `.agents/skills`.

Each run is given the selection the agent manager would build from that folder.

Every test drives a real run through a scripted `FunctionModel`, so they pin what the
model sees and receives rather than how the runner builds it: the skill catalog, the
instructions a loaded skill delivers, and what the read-only file tools reach. Catalog
entries and loaded instructions are read out of pydantic-ai's framing, which belongs to
pydantic-ai rather than the runner. The file tools themselves belong to harness, so the
tests here cover only what the runner decides: which files they can reach.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend

from griptape_nodes.agents.pydantic_ai.runner import PydanticAgentRunner
from griptape_nodes.agents.skills import discover_skills, select_skills
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# The read-only `FileSystem` tools harness offers alongside skills.
FILE_TOOLS = {"file_info", "find_files", "list_directory", "read_file", "search_files"}

CATALOG_PREAMBLE = (
    "The following capabilities are deferred and can be loaded using the `load_capability` tool. "
    "A capability's tools stay hidden until it is loaded:\n"
)

_LOADED_CAPABILITY = re.compile(
    r'<context id="capability:(?P<name>[^"]+)">\n(?P<instructions>.*)\n</context>', re.DOTALL
)


@dataclass
class _Run:
    """What the model saw on each step of a scripted run, and what each scripted call returned."""

    steps: list[AgentInfo] = field(default_factory=list)
    results: list[Any] = field(default_factory=list)
    loaded: dict[str, str] = field(default_factory=dict)
    """The instructions each loaded skill delivered, by skill name."""


def _runner(workspace: Path, threads_dir: Path) -> PydanticAgentRunner:
    """Build a runner rooted at `workspace` without touching Griptape Cloud."""
    storage = LocalThreadStorageDriver(threads_dir, config_manager=None, secrets_manager=None)  # type: ignore[arg-type]
    return PydanticAgentRunner(model_name="test", api_key="dummy", workspace_root=workspace, storage=storage)


async def _drive(
    runner: PydanticAgentRunner,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    offer_skills: bool = True,
    libraries: list[Path] | None = None,
) -> _Run:
    """Run `runner` with a model that makes `calls` one per step, then answers "done".

    Returns the tools and instructions the model saw on each step, for each call either
    the tool's return content or the retry prompt it was answered with, and the
    instructions each loaded skill delivered. Skills come from `libraries`, by default the
    workspace's `.agents/skills`.
    """
    run = _Run()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        step = len(run.steps)
        run.steps.append(info)
        run.loaded.update(_loaded_instructions(messages))
        if step < len(calls):
            name, args = calls[step]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call-{step}")}
            return
        yield "done"

    skill_folders: list[Path] = []
    if offer_skills:
        if libraries is None:
            libraries = [runner.workspace_root / ".agents/skills"]
        skill_folders = [skill.folder for skill in select_skills(await discover_skills(libraries), disabled=set())]

    runner._agent = Agent(FunctionModel(stream_function=stream))
    result = await runner.run("Go.", skill_folders=skill_folders)

    for message in runner.storage.load_history(result.thread_id):
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, (ToolReturnPart, RetryPromptPart)):
                run.results.append(part.content)
    return run


def _write_skill(
    workspace: Path,
    name: str,
    body: str = "Guidance for the task.",
    description: str | None = None,
) -> Path:
    """Create a valid `.agents/skills/<name>/SKILL.md` under `workspace`."""
    skill_dir = workspace / ".agents/skills" / name
    skill_dir.mkdir(parents=True)
    description = description if description is not None else f"{name} description"
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}")
    return skill_dir


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def _loaded_instructions(messages: list[ModelMessage]) -> dict[str, str]:
    """The instructions each loaded skill delivered in `messages`, by skill name."""
    loaded: dict[str, str] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                loaded.update({m["name"]: m["instructions"] for m in _LOADED_CAPABILITY.finditer(part.content)})
    return loaded


def _catalog(info: AgentInfo) -> str | None:
    """The skill entries in the catalog the model saw, or `None` when it saw no catalog."""
    if info.instructions is None or CATALOG_PREAMBLE not in info.instructions:
        return None
    entries = info.instructions.split(CATALOG_PREAMBLE, 1)[1]
    return entries.split("\n</context>", 1)[0]


def _tool_names(info: AgentInfo) -> set[str]:
    return {tool.name for tool in info.function_tools}


def _load(name: str) -> tuple[str, dict[str, Any]]:
    return ("load_capability", {"id": name})


def _read(path: str) -> tuple[str, dict[str, Any]]:
    return ("read_file", {"path": path})


def _list(path: str) -> tuple[str, dict[str, Any]]:
    return ("list_directory", {"path": path})


def _file_text(result: Any) -> str:
    """The file content in a `read_file` result, without its header and line numbers."""
    lines = str(result).splitlines()[1:]
    return "\n".join(line.split("\t", 1)[1] for line in lines)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace root."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def threads(tmp_path: Path) -> Path:
    """Where the runner stores threads."""
    return tmp_path / "threads"


@pytest.mark.asyncio
async def test_no_skills_when_skills_dir_missing(workspace: Path, threads: Path) -> None:
    """A workspace with no `.agents/skills` dir offers no skills."""
    run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == set()
    assert _catalog(run.steps[0]) is None


@pytest.mark.asyncio
async def test_no_skills_without_a_selection(workspace: Path, threads: Path) -> None:
    """A run given no selection offers no skills even when skills exist."""
    _write_skill(workspace, "demo-skill")

    run = await _drive(_runner(workspace, threads), [], offer_skills=False)

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
async def test_catalog_lists_each_skill(workspace: Path, threads: Path) -> None:
    """Every skill is a deferred capability named and described in the catalog."""
    _write_skill(workspace, "alpha", description="Alpha does\n  folded work.")
    _write_skill(workspace, "beta", description="|\n  Beta line one.\n  Beta line two.")

    run = await _drive(_runner(workspace, threads), [])

    assert _catalog(run.steps[0]) == "- alpha: Alpha does folded work.\n- beta: Beta line one.\n  Beta line two."


@pytest.mark.asyncio
async def test_skills_come_with_read_only_file_tools(workspace: Path, threads: Path) -> None:
    """Skills add `load_capability` and the file tools that only read."""
    _write_skill(workspace, "demo-skill")

    run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == {"load_capability", *FILE_TOOLS}


@pytest.mark.asyncio
async def test_skill_hidden_from_the_model(workspace: Path, threads: Path) -> None:
    """A skill whose frontmatter sets `disable-model-invocation: true` is left out of the catalog."""
    _write_skill(workspace, "shown")
    _write(
        workspace / ".agents/skills/hidden/SKILL.md",
        "---\nname: hidden\ndescription: Hidden.\ndisable-model-invocation: true\n---\n\nBody.",
    )

    run = await _drive(_runner(workspace, threads), [])

    assert _catalog(run.steps[0]) == "- shown: shown description"


@pytest.mark.asyncio
async def test_no_tools_when_every_skill_is_hidden(workspace: Path, threads: Path) -> None:
    """With no skill to offer, the run gets no catalog and no file tools."""
    _write(
        workspace / ".agents/skills/hidden/SKILL.md",
        "---\nname: hidden\ndescription: Hidden.\ndisable-model-invocation: true\n---\n\nBody.",
    )

    run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
async def test_loaded_instructions(workspace: Path, threads: Path) -> None:
    """Loading a skill returns its heading, the directory its relative paths resolve against, and its body."""
    _write_skill(workspace, "demo-skill", body="Read references/notes.md.\n\nDone.")

    run = await _drive(_runner(workspace, threads), [_load("demo-skill")])

    assert run.loaded == {
        "demo-skill": (
            "# Skill: demo-skill\n\n"
            "Skill directory: `/demo-skill`. Relative paths in this skill resolve against it.\n\n"
            "Read references/notes.md.\n\nDone."
        )
    }


@pytest.mark.asyncio
async def test_loaded_instructions_without_body(workspace: Path, threads: Path) -> None:
    """A skill with no body loads as its heading and directory alone."""
    _write_skill(workspace, "demo-skill", body="")

    run = await _drive(_runner(workspace, threads), [_load("demo-skill")])

    assert run.loaded == {
        "demo-skill": (
            "# Skill: demo-skill\n\nSkill directory: `/demo-skill`. Relative paths in this skill resolve against it."
        )
    }


@pytest.mark.asyncio
async def test_reading_skill_files(workspace: Path, threads: Path) -> None:
    """The file tools reach every file a skill ships: documents, scripts, nested `SKILL.md` files, and images."""
    skill_dir = _write_skill(workspace, "demo-skill")
    _write(skill_dir / "references/notes.md", "notes")
    _write(skill_dir / "scripts/go.py", "print(1)")
    _write(skill_dir / "examples/child/SKILL.md", "a nested example")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    _write(skill_dir / "pic.png", png)

    run = await _drive(
        _runner(workspace, threads),
        [
            _load("demo-skill"),
            _read("demo-skill/references/notes.md"),
            _read("demo-skill/scripts/go.py"),
            _read("demo-skill/examples/child/SKILL.md"),
            _read("demo-skill/pic.png"),
        ],
    )

    assert _file_text(run.results[1]) == "notes"
    assert _file_text(run.results[2]) == "print(1)"
    assert _file_text(run.results[3]) == "a nested example"
    image = run.results[4][1]
    assert isinstance(image, BinaryContent)
    assert image.data == png


@pytest.mark.asyncio
async def test_files_cannot_leave_their_skill(workspace: Path, threads: Path, tmp_path: Path) -> None:
    """A symlink inside a skill that points out of it reads as missing and is left out of listings."""
    skill_dir = _write_skill(workspace, "demo-skill")
    _write(skill_dir / "notes.md", "notes")
    outside = _write(tmp_path / "outside/secret.md", "secret")
    (skill_dir / "escape.md").symlink_to(outside)
    (skill_dir / "escape-dir").symlink_to(outside.parent, target_is_directory=True)
    other_dir = _write_skill(workspace, "other-skill")
    _write(other_dir / "private.md", "private")
    (skill_dir / "sibling.md").symlink_to(other_dir / "private.md")

    run = await _drive(
        _runner(workspace, threads),
        [
            _list("demo-skill"),
            _read("demo-skill/escape.md"),
            _read("demo-skill/notes.md"),
            _read("demo-skill/escape-dir/secret.md"),
            _read("demo-skill/notes.md"),
            _read("demo-skill/sibling.md"),
            _read("demo-skill/notes.md"),
            _read("../outside/secret.md"),
        ],
    )

    assert "escape" not in str(run.results[0])
    assert "sibling" not in str(run.results[0])
    assert "notes.md" in str(run.results[0])
    assert [run.results[index] for index in (1, 3, 5, 7)] == [
        "Path not found: demo-skill/escape.md",
        "Path not found: demo-skill/escape-dir/secret.md",
        "Path not found: demo-skill/sibling.md",
        "Path not found: ../outside/secret.md",
    ]
    assert _file_text(run.results[2]) == "notes"


@pytest.mark.asyncio
async def test_only_offered_skills_are_reachable(workspace: Path, threads: Path, tmp_path: Path) -> None:
    """The file tools reach the offered skills' folders and nothing else in the skills folder."""
    _write_skill(workspace, "demo-skill")
    _write(
        workspace / ".agents/skills/hidden/SKILL.md",
        "---\nname: hidden\ndescription: Hidden.\ndisable-model-invocation: true\n---\n\nBody.",
    )
    _write(workspace / ".agents/skills/README.md", "about these skills")
    _write(tmp_path / "outside/secret.md", "secret")
    (workspace / ".agents/skills/stray").symlink_to(tmp_path / "outside", target_is_directory=True)

    run = await _drive(
        _runner(workspace, threads),
        [
            _list("/"),
            _read("hidden/SKILL.md"),
            _read("demo-skill/SKILL.md"),
            _read("stray/secret.md"),
            _read("demo-skill/SKILL.md"),
            _read("README.md"),
        ],
    )

    assert run.results[0] == "demo-skill/"
    assert run.results[1] == "Path not found: hidden/SKILL.md"
    assert run.results[3] == "Path not found: stray/secret.md"
    assert run.results[5] == "Path not found: README.md"


@pytest.mark.asyncio
async def test_skills_from_several_folders(workspace: Path, threads: Path, tmp_path: Path) -> None:
    """Skills from different skills folders share one run, each at the path its instructions name."""
    own = _write_skill(workspace, "own-skill")
    _write(own / "notes.md", "own notes")
    shared = _write_skill(tmp_path / "studio", "shared-skill")
    _write(shared / "notes.md", "shared notes")

    run = await _drive(
        _runner(workspace, threads),
        [_load("shared-skill"), _read("own-skill/notes.md"), _read("shared-skill/notes.md")],
        libraries=[workspace / ".agents/skills", tmp_path / "studio/.agents/skills"],
    )

    assert _catalog(run.steps[0]) == "- own-skill: own-skill description\n- shared-skill: shared-skill description"
    assert "Skill directory: `/shared-skill`." in run.loaded["shared-skill"]
    assert _file_text(run.results[1]) == "own notes"
    assert _file_text(run.results[2]) == "shared notes"


@pytest.mark.asyncio
async def test_a_skill_whose_folder_name_differs_in_unicode_form(workspace: Path, threads: Path) -> None:
    """A skill is reached at its folder's spelling, which is what its instructions name."""
    folder = workspace / ".agents/skills/\ufb01le-skill"
    _write(folder / "SKILL.md", "---\nname: file-skill\ndescription: Files.\n---\n\nBody.")
    _write(folder / "notes.md", "notes")

    run = await _drive(_runner(workspace, threads), [_load("file-skill"), _read("\ufb01le-skill/notes.md")])

    assert "Skill directory: `/\ufb01le-skill`." in run.loaded["file-skill"]
    assert _file_text(run.results[1]) == "notes"


@pytest.mark.asyncio
async def test_a_skill_folder_can_be_a_symlink(workspace: Path, threads: Path, tmp_path: Path) -> None:
    """A skill folder linked in from elsewhere is offered, and its files are readable."""
    shared = _write_skill(tmp_path / "shared", "shared-skill")
    _write(shared / "references/notes.md", "notes")
    library = workspace / ".agents/skills"
    library.mkdir(parents=True)
    (library / "shared-skill").symlink_to(shared, target_is_directory=True)

    run = await _drive(
        _runner(workspace, threads),
        [_load("shared-skill"), _read("shared-skill/references/notes.md")],
    )

    assert _catalog(run.steps[0]) == "- shared-skill: shared-skill description"
    assert _file_text(run.results[1]) == "notes"


@pytest.mark.asyncio
async def test_spec_gaps_are_logged(workspace: Path, threads: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An overlong description and ignored frontmatter fields are logged, and the skill is still offered."""
    _write_skill(workspace, "long-skill", description="x" * 1025)
    _write(
        workspace / ".agents/skills/tools-skill/SKILL.md",
        "---\nname: tools-skill\ndescription: Tools.\nallowed-tools: Bash\nmodel: opus\n---\n\nBody.",
    )

    with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
        run = await _drive(_runner(workspace, threads), [])

    assert _catalog(run.steps[0]) == f"- long-skill: {'x' * 1025}\n- tools-skill: Tools."
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Skill 'long-skill'" in message and "1025 characters, over the 1024-character limit" in message
        for message in messages
    )
    assert any("Skill 'tools-skill'" in message and "ignores: allowed-tools, model." in message for message in messages)


@pytest.mark.asyncio
async def test_broken_skill_costs_only_itself(workspace: Path, threads: Path) -> None:
    """A `SKILL.md` with bad frontmatter is left out; the valid skills beside it still load."""
    _write_skill(workspace, "good-skill")
    _write(workspace / ".agents/skills/broken-skill/SKILL.md", "no frontmatter at all\n")

    run = await _drive(_runner(workspace, threads), [_load("good-skill")])

    assert _catalog(run.steps[0]) == "- good-skill: good-skill description"
    assert set(run.loaded) == {"good-skill"}


@pytest.mark.asyncio
async def test_no_skills_when_every_skill_is_broken(workspace: Path, threads: Path) -> None:
    """A library where nothing loads offers no skills, and the run still completes."""
    _write(workspace / ".agents/skills/broken-skill/SKILL.md", "no frontmatter at all\n")

    run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions that bind this user")
async def test_unreadable_skill_costs_skills_not_the_run(workspace: Path, threads: Path) -> None:
    """A `SKILL.md` that cannot be read costs the skills, not the run."""
    skill_md = _write_skill(workspace, "demo-skill") / "SKILL.md"
    skill_md.chmod(0)
    try:
        run = await _drive(_runner(workspace, threads), [])
    finally:
        skill_md.chmod(0o644)

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
async def test_skills_load_without_a_posix_workspace(workspace: Path, threads: Path) -> None:
    """Skills load and their files read where `LocalWorkspaceBackend` cannot be built, as on Windows."""
    skill_dir = _write_skill(workspace, "demo-skill")
    _write(skill_dir / "references/notes.md", "notes")
    unsupported = NotImplementedError("`LocalWorkspaceBackend` only supports POSIX platforms at the moment")

    with patch.object(LocalWorkspaceBackend, "__init__", side_effect=unsupported):
        run = await _drive(
            _runner(workspace, threads),
            [_load("demo-skill"), _read("demo-skill/references/notes.md")],
        )

    assert _file_text(run.results[1]) == "notes"


@pytest.mark.asyncio
async def test_each_run_reads_skills_again(workspace: Path, threads: Path) -> None:
    """A skill added between runs is offered on the next run without a restart."""
    _write_skill(workspace, "first-skill")
    runner = _runner(workspace, threads)
    first = await _drive(runner, [])

    _write_skill(workspace, "second-skill")
    second = await _drive(runner, [])

    assert _catalog(first.steps[0]) == "- first-skill: first-skill description"
    assert _catalog(second.steps[0]) == (
        "- first-skill: first-skill description\n- second-skill: second-skill description"
    )
