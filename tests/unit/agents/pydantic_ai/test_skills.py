"""Tests for the skills the runner offers the model from `.agents/skills`.

Every test drives a real run through a scripted `FunctionModel`, so they pin what the
model sees and receives rather than how the runner builds it: the skill catalog, the
`read_skill_resource` tool, the instructions `load_capability` returns, and what each
resource read returns or retries with.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from griptape_nodes.agents.pydantic_ai.runner import PydanticAgentRunner
from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

READ_SKILL_RESOURCE_DESCRIPTION = (
    "<summary>Read a supplementary file bundled with a skill.\n\n"
    "Skill packages ship reference documentation, templates, schemas and data files\n"
    "alongside their instructions. A skill's instructions name the ones it needs;\n"
    "this tool reads them.\n\n"
    "When to use this:\n"
    "- When a loaded skill's instructions reference a specific file\n"
    "- To access form templates, reference documentation, or data schemas\n"
    "- When you need supplementary information beyond the skill instructions</summary>\n"
    "<returns>\n<description>The resource content as a string.</description>\n</returns>"
)

READ_SKILL_RESOURCE_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "skill_name": {
            "description": "Name of the skill containing the resource, exactly as it\nappears in the capability catalog.",
            "type": "string",
        },
        "resource_name": {
            "description": (
                "Path of the resource relative to the skill directory, as\n"
                'listed under "Bundled files" in the loaded skill\'s instructions.\n'
                'Examples: "FORMS.md", "references/REFERENCE.md", "get_schema"\n'
                "A file name on its own works when only one resource has it."
            ),
            "type": "string",
        },
        "args": {
            "anyOf": [{"additionalProperties": True, "type": "object"}, {"type": "null"}],
            "default": None,
            "description": (
                "Arguments for callable resources (optional for static files).\n"
                "Keys must match the parameter names in the resource's schema."
            ),
        },
    },
    "required": ["skill_name", "resource_name"],
    "type": "object",
}

CATALOG_PREAMBLE = (
    "The following capabilities are deferred and can be loaded using the `load_capability` tool. "
    "A capability's tools stay hidden until it is loaded:"
)


@dataclass
class _Run:
    """What the model saw on each step of a scripted run, and what each scripted call returned."""

    steps: list[AgentInfo] = field(default_factory=list)
    results: list[Any] = field(default_factory=list)


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


async def _drive(runner: PydanticAgentRunner, calls: list[tuple[str, dict[str, Any]]]) -> _Run:
    """Run `runner` with a model that makes `calls` one per step, then answers "done".

    Returns the tools and instructions the model saw on each step, and for each call
    either the tool's return content or the retry prompt it was answered with.
    """
    run = _Run()

    async def stream(_messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        step = len(run.steps)
        run.steps.append(info)
        if step < len(calls):
            name, args = calls[step]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call-{step}")}
            return
        yield "done"

    runner._agent = Agent(FunctionModel(stream_function=stream))
    result = await runner.run("Go.")

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


def _tool_names(info: AgentInfo) -> set[str]:
    return {tool.name for tool in info.function_tools}


def _load(name: str) -> tuple[str, dict[str, Any]]:
    return ("load_capability", {"id": name})


def _read(skill: str, resource: str) -> tuple[str, dict[str, Any]]:
    return ("read_skill_resource", {"skill_name": skill, "resource_name": resource})


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
    assert run.steps[0].instructions is None


@pytest.mark.asyncio
async def test_no_skills_when_disabled(workspace: Path, threads: Path) -> None:
    """`auto_load_skills=False` offers no skills even when skills exist."""
    _write_skill(workspace, "demo-skill")

    run = await _drive(_runner(workspace, threads, auto_load_skills=False), [])

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
async def test_catalog_lists_each_skill(workspace: Path, threads: Path) -> None:
    """Every skill is a deferred capability named and described in the catalog."""
    _write_skill(workspace, "alpha", description="Alpha does\n  folded work.")
    _write_skill(workspace, "beta", description="|\n  Beta line one.\n  Beta line two.")

    run = await _drive(_runner(workspace, threads), [])

    assert run.steps[0].instructions == (
        f"{CATALOG_PREAMBLE}\n- alpha: Alpha does folded work.\n- beta: Beta line one.\nBeta line two."
    )


@pytest.mark.asyncio
async def test_no_resource_tool_without_bundled_files(workspace: Path, threads: Path) -> None:
    """Skills that ship only a `SKILL.md` get `load_capability` and no resource tool."""
    _write_skill(workspace, "demo-skill")

    run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == {"load_capability"}


@pytest.mark.asyncio
async def test_resource_tool_definition(workspace: Path, threads: Path) -> None:
    """The resource tool's name, description, and schema are what the model is given."""
    skill_dir = _write_skill(workspace, "demo-skill")
    _write(skill_dir / "references/notes.md", "Reference material.")

    run = await _drive(_runner(workspace, threads), [])

    tools = {tool.name: tool for tool in run.steps[0].function_tools}
    assert set(tools) == {"load_capability", "read_skill_resource"}
    tool = tools["read_skill_resource"]
    assert tool.description == READ_SKILL_RESOURCE_DESCRIPTION
    assert tool.parameters_json_schema == READ_SKILL_RESOURCE_SCHEMA
    assert tool.strict is None
    assert tool.kind == "function"


@pytest.mark.asyncio
async def test_loaded_instructions(workspace: Path, threads: Path) -> None:
    """Loading a skill returns its heading, its body with placeholders resolved, and its file list."""
    skill_dir = _write_skill(workspace, "demo-skill", body="See ${SKILL_DIR}/x and ${CLAUDE_SKILL_DIR}.\n\nDone.")
    _write(skill_dir / "references/notes.md", "Reference material.")
    _write(skill_dir / "data.json", "{}")

    run = await _drive(_runner(workspace, threads), [_load("demo-skill")])

    directory = skill_dir.resolve()
    assert run.results == [
        {
            "instructions": (
                f"# Skill: demo-skill\n\nSee {directory}/x and {directory}.\n\nDone.\n\n"
                "## Bundled files\n\n"
                "Read with `read_skill_resource`, using these exact `resource_name` values:\n\n"
                "- `data.json`\n- `references/notes.md`"
            )
        }
    ]


@pytest.mark.asyncio
async def test_loaded_instructions_without_files_or_body(workspace: Path, threads: Path) -> None:
    """A skill with no body and no files loads as its heading alone."""
    _write_skill(workspace, "demo-skill", body="")

    run = await _drive(_runner(workspace, threads), [_load("demo-skill")])

    assert run.results == [{"instructions": "# Skill: demo-skill"}]


@pytest.mark.asyncio
async def test_file_list_is_truncated(workspace: Path, threads: Path) -> None:
    """Past 50 files the list ends with a count of the rest."""
    skill_dir = _write_skill(workspace, "demo-skill", body="")
    for index in range(53):
        _write(skill_dir / f"references/{index:02d}.md", "x")

    run = await _drive(_runner(workspace, threads), [_load("demo-skill")])

    listed = "\n".join(f"- `references/{index:02d}.md`" for index in range(50))
    assert run.results == [
        {
            "instructions": (
                "# Skill: demo-skill\n\n## Bundled files\n\n"
                "Read with `read_skill_resource`, using these exact `resource_name` values:\n\n"
                f"{listed}\n- ...and 3 more"
            )
        }
    ]


@pytest.mark.asyncio
async def test_which_files_are_resources(workspace: Path, threads: Path, tmp_path: Path) -> None:
    """Text files are resources; scripts, binaries, build noise, and escaping links are not."""
    skill_dir = _write_skill(workspace, "demo-skill", body="")
    _write(skill_dir / "references/notes.md", "notes")
    _write(skill_dir / "deep/nested/page.txt", "page")
    _write(skill_dir / "config.yaml", "a: 1")
    _write(skill_dir / "scripts/go.py", "print(1)")
    _write(skill_dir / "scripts/README.md", "about the scripts")
    _write(skill_dir / "helper.sh", "echo hi")
    _write(skill_dir / "image.png", b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    _write(skill_dir / "__pycache__/mod.cpython-312.pyc", "x")
    _write(skill_dir / "stale.pyc", "x")
    _write(skill_dir / ".DS_Store", "x")
    outside = _write(tmp_path / "outside.md", "secret")
    (skill_dir / "escape.md").symlink_to(outside)
    if os.name == "posix":
        executable = _write(skill_dir / "tool", "#!/bin/sh\necho hi\n")
        executable.chmod(0o755)

    with pytest.warns(UserWarning, match="symlink escape"):
        run = await _drive(_runner(workspace, threads), [_load("demo-skill")])

    instructions = run.results[0]["instructions"]
    listed = [line[3:-1] for line in instructions.splitlines() if line.startswith("- `")]
    assert listed == ["config.yaml", "deep/nested/page.txt", "references/notes.md", "scripts/README.md"]


@pytest.mark.asyncio
async def test_reading_resources(workspace: Path, threads: Path) -> None:
    """Reads resolve exact and short names, parse JSON and YAML, and explain every failure."""
    skill_dir = _write_skill(workspace, "demo-skill", body="")
    _write(skill_dir / "references/notes.md", "notes")
    _write(skill_dir / "data.json", '{"a": [1, 2]}')
    _write(skill_dir / "broken.json", "{not json")
    _write(skill_dir / "config.yaml", "a: 1\nb: [x, y]")
    _write(skill_dir / "a/dup.md", "first")
    _write(skill_dir / "b/dup.md", "second")
    _write_skill(workspace, "other-skill")

    # One failure per tool is allowed before a success, so failures alternate with reads that work.
    run = await _drive(
        _runner(workspace, threads),
        [
            _read("demo-skill", "references/notes.md"),
            _load("demo-skill"),
            _read("demo-skill", "references/notes.md"),
            _read("missing-skill", "notes.md"),
            _read("demo-skill", "notes"),
            _read("demo-skill", "dup.md"),
            _read("demo-skill", "notes.md"),
            _read("demo-skill", "nowhere.md"),
            _read("demo-skill", "data.json"),
            _read("demo-skill", "broken.json"),
            _read("demo-skill", "config.yaml"),
            _read("other-skill", "notes.md"),
            _read("demo-skill", "a/dup.md"),
            _load("other-skill"),
            _read("other-skill", "notes.md"),
        ],
    )

    assert run.results[0] == (
        "Skill 'demo-skill' is not loaded. Call load_capability with id='demo-skill' first, then read its files."
    )
    assert run.results[2] == "notes"
    # The list names every skill, including ones that ship no files.
    assert run.results[3] == (
        "Skill 'missing-skill' has no bundled files available. Skills with bundled files: demo-skill, other-skill. "
        "Use the exact skill name."
    )
    assert run.results[4] == "notes"
    assert run.results[5] == (
        "Resource 'dup.md' is ambiguous in skill 'demo-skill': ['a/dup.md', 'b/dup.md']. "
        "Use the full path relative to the skill directory."
    )
    assert run.results[6] == "notes"
    assert run.results[7] == (
        "Resource 'nowhere.md' not found in skill 'demo-skill'. Available: ['a/dup.md', 'b/dup.md', "
        "'broken.json', 'config.yaml', 'data.json', 'references/notes.md']. "
        "Use the exact name from the skill instructions."
    )
    assert run.results[8] == {"a": [1, 2]}
    assert run.results[9] == "{not json"
    assert run.results[10] == {"a": 1, "b": ["x", "y"]}
    assert run.results[11] == (
        "Skill 'other-skill' is not loaded. Call load_capability with id='other-skill' first, then read its files."
    )
    assert run.results[12] == "first"
    assert run.results[14] == (
        "Resource 'notes.md' not found in skill 'other-skill'. Available: []. "
        "Use the exact name from the skill instructions."
    )


@pytest.mark.asyncio
async def test_broken_skill_costs_only_itself(workspace: Path, threads: Path) -> None:
    """A `SKILL.md` with bad frontmatter is left out; the valid skills beside it still load."""
    skill_dir = _write_skill(workspace, "good-skill")
    _write(skill_dir / "references/notes.md", "Reference material.")
    _write(workspace / ".agents/skills/broken-skill/SKILL.md", "no frontmatter at all\n")
    _write(workspace / ".agents/skills/broken-skill/references/notes.md", "Unreachable.")

    run = await _drive(_runner(workspace, threads), [_load("good-skill")])

    assert run.steps[0].instructions == f"{CATALOG_PREAMBLE}\n- good-skill: good-skill description"
    assert run.results == [
        {
            "instructions": (
                "# Skill: good-skill\n\nGuidance for the task.\n\n## Bundled files\n\n"
                "Read with `read_skill_resource`, using these exact `resource_name` values:\n\n"
                "- `references/notes.md`"
            )
        }
    ]


@pytest.mark.asyncio
async def test_no_skills_when_every_skill_is_broken(workspace: Path, threads: Path) -> None:
    """A library where nothing loads offers no skills, and the run still completes."""
    _write(workspace / ".agents/skills/broken-skill/SKILL.md", "no frontmatter at all\n")

    run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
async def test_unreadable_skill_costs_skills_not_the_run(workspace: Path, threads: Path) -> None:
    """A `SKILL.md` that cannot be read costs the skills, not the run."""
    _write_skill(workspace, "demo-skill")
    real_read_text = Path.read_text

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "SKILL.md":
            raise PermissionError(13, "Permission denied")
        return real_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", read_text):
        run = await _drive(_runner(workspace, threads), [])

    assert _tool_names(run.steps[0]) == set()


@pytest.mark.asyncio
async def test_skills_load_without_a_posix_workspace(workspace: Path, threads: Path) -> None:
    """Skills load where `LocalWorkspaceBackend` cannot be built, as on Windows."""
    skill_dir = _write_skill(workspace, "demo-skill")
    _write(skill_dir / "references/notes.md", "notes")
    LocalWorkspaceBackend = pytest.importorskip("pydantic_ai.workspaces").LocalWorkspaceBackend  # noqa: N806
    unsupported = NotImplementedError("`LocalWorkspaceBackend` only supports POSIX platforms at the moment")

    with patch.object(LocalWorkspaceBackend, "__init__", side_effect=unsupported):
        run = await _drive(_runner(workspace, threads), [_load("demo-skill"), _read("demo-skill", "notes.md")])

    assert run.results[1] == "notes"


@pytest.mark.asyncio
async def test_each_run_reads_skills_again(workspace: Path, threads: Path) -> None:
    """A skill added between runs is offered on the next run without a restart."""
    _write_skill(workspace, "first-skill")
    runner = _runner(workspace, threads)
    first = await _drive(runner, [])

    _write_skill(workspace, "second-skill")
    second = await _drive(runner, [])

    assert first.steps[0].instructions == f"{CATALOG_PREAMBLE}\n- first-skill: first-skill description"
    assert second.steps[0].instructions == (
        f"{CATALOG_PREAMBLE}\n- first-skill: first-skill description\n- second-skill: second-skill description"
    )
