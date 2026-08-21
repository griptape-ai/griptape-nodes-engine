"""Effective project paths, exercised through a real Engine against real files on disk.

The rest of the project-manager suite calls handlers directly with a mocked ConfigManager, which is
the right harness for the resolution ladders but structurally cannot catch three things:

- a handler that was never registered (nothing else dispatches ListProjectTemplatesRequest through
  the event system, so a wiring mistake would not fail the suite),
- a config layer read from the wrong file, since the layers are real here rather than stubbed,
- the listing disagreeing with the provisioning preview about where libraries live.

So these tests build an actual Engine, write actual project YAML, and go through handle_request.
"""

import json
from pathlib import Path

import pytest

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.library_events import (
    LibraryProvisioningActionKind,
    PreviewProjectProvisioningRequest,
    PreviewProjectProvisioningResultFailure,
    PreviewProjectProvisioningResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    ListProjectTemplatesRequest,
    ListProjectTemplatesResultSuccess,
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultFailure,
    LoadProjectTemplateResultSuccess,
    ProjectTemplateInfo,
)

PROJECT_FILE_NAME = "griptape-nodes-project.yml"
ADJACENT_CONFIG_NAME = "griptape_nodes_config.json"
LIBS_ENV_VAR = "GTN_TEST_STUDIO_LIBS"

# A variable whose VALUE is itself a reference. Ordinary in a `.env` read without interpolation, and
# the shape that one expansion pass leaves half-resolved.
CHAIN_OUTER_ENV_VAR = "GTN_TEST_CHAIN_OUTER"
CHAIN_INNER_ENV_VAR = "GTN_TEST_CHAIN_INNER"
CYCLE_A_ENV_VAR = "GTN_TEST_CYCLE_A"
CYCLE_B_ENV_VAR = "GTN_TEST_CYCLE_B"

# A pinned download entry proves the provisioning preview probes the SAME libraries root the listing
# reports. The preview payload carries no path, but its installed-version probe READS one, so the
# plan's kind is an observable proxy for the directory it looked in.
LIB_GIT_URL = "https://github.com/example/gtn-test-lib"
LIB_DIR_NAME = "gtn-test-lib"  # extract_repo_name_from_url(LIB_GIT_URL); no `name` key, so the probe uses it
VERSION_AT_DECLARED_ROOT = "1.2.3"
VERSION_AT_WORKSPACE_DEFAULT = "9.9.9"

DECLARED_LIBRARIES_YAML = f"""project_template_schema_version: "0.3.3"
name: Studio Base
libraries_dir: "${{{LIBS_ENV_VAR}}}/shared-libraries"
"""
CHILD_OF_BASE_YAML = f"""project_template_schema_version: "0.3.3"
name: Shot sc010
parent_project_path: "../../{PROJECT_FILE_NAME}"
"""
DECLARES_NOTHING_YAML = """project_template_schema_version: "0.3.3"
name: Solo
"""
CHAINED_LIBRARIES_YAML = f"""project_template_schema_version: "0.3.3"
name: Studio Chained
libraries_dir: "${{{CHAIN_OUTER_ENV_VAR}}}/shared-libraries"
"""
CYCLIC_LIBRARIES_YAML = f"""project_template_schema_version: "0.3.3"
name: Studio Cyclic
libraries_dir: "${{{CYCLE_A_ENV_VAR}}}/shared-libraries"
"""


def write_project(project_dir: Path, yaml_text: str) -> Path:
    """Write a project YAML at its canonical filename and return the path."""
    project_dir.mkdir(parents=True, exist_ok=True)
    project_path = project_dir / PROJECT_FILE_NAME
    project_path.write_text(yaml_text, encoding="utf-8")
    return project_path


def write_pinned_download(project_dir: Path, version_spec: str) -> None:
    """Pin one libraries_to_download entry in a project's adjacent config.

    The key lives under the app_events tree (LIBRARIES_TO_DOWNLOAD_KEY), not at the config root.
    """
    (project_dir / ADJACENT_CONFIG_NAME).write_text(
        json.dumps(
            {
                "app_events": {
                    "on_app_initialization_complete": {
                        "libraries_to_download": [{"git_url": LIB_GIT_URL, "version": version_spec}]
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def plant_installed_library(libraries_root: Path, version: str) -> None:
    """Write the minimal installed-library manifest the provisioning version probe reads."""
    manifest_dir = libraries_root / LIB_DIR_NAME
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "griptape_nodes_library.json").write_text(
        json.dumps({"name": "GTN Test Lib", "metadata": {"library_version": version}}),
        encoding="utf-8",
    )


class TestEffectiveProjectPathsThroughEngine:
    """The listing reports where each project's files and libraries actually land."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """The engine's global workspace, empty of any project."""
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    @pytest.fixture
    def studio_dir(self, tmp_path: Path) -> Path:
        """The off-workspace volume a project points its libraries at through an env var."""
        return tmp_path / "studio"

    @pytest.fixture
    def engine(self, monkeypatch: pytest.MonkeyPatch, workspace: Path, studio_dir: Path) -> Engine:
        """A real Engine whose config layers see only this test's workspace and variable.

        Built AFTER the env vars are set, because ConfigManager loads its layers during construction.
        """
        monkeypatch.setenv("GTN_CONFIG_WORKSPACE_DIRECTORY", str(workspace))
        monkeypatch.setenv(LIBS_ENV_VAR, str(studio_dir))
        return Engine()

    @staticmethod
    def load(engine: Engine, project_path: Path) -> LoadProjectTemplateResultSuccess | LoadProjectTemplateResultFailure:
        """Register a project through the event system, as the boot path and the GUI both do."""
        result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_path))
        assert isinstance(result, LoadProjectTemplateResultSuccess | LoadProjectTemplateResultFailure)
        return result

    @staticmethod
    def listing(engine: Engine) -> ListProjectTemplatesResultSuccess:
        result = engine.handle_request(ListProjectTemplatesRequest(include_system_builtins=False))
        assert isinstance(result, ListProjectTemplatesResultSuccess)
        return result

    @staticmethod
    def loaded_by_path(listing: ListProjectTemplatesResultSuccess) -> dict[str, ProjectTemplateInfo]:
        return {info.project_file_path: info for info in listing.successfully_loaded if info.project_file_path}

    @staticmethod
    def libraries_root_of(info: ProjectTemplateInfo) -> str:
        """The reported libraries root of a project that loaded.

        The field is optional on the payload for entries that have no project file or failed to load;
        asserting it here is the contract every case below relies on -- a loaded, file-backed project
        always names a root.
        """
        assert info.libraries_root is not None
        return info.libraries_root

    def test_declared_libraries_dir_is_reported_expanded_and_absolute(
        self, engine: Engine, tmp_path: Path, studio_dir: Path
    ) -> None:
        """An env-var libraries_dir resolves against the variable, not the project directory.

        Expansion happens before the relative-vs-absolute decision, so a variable holding an absolute
        path is reported as that path and never joined onto the project directory.
        """
        base_path = write_project(tmp_path / "base", DECLARED_LIBRARIES_YAML)

        assert isinstance(self.load(engine, base_path), LoadProjectTemplateResultSuccess)
        info = self.loaded_by_path(self.listing(engine))[str(base_path)]

        libraries_root = self.libraries_root_of(info)
        assert libraries_root == str(canonicalize_for_identity(studio_dir / "shared-libraries"))
        assert str(base_path.parent) not in libraries_root
        assert LIBS_ENV_VAR not in libraries_root

    def test_a_variable_whose_value_is_itself_a_reference_resolves_all_the_way(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, studio_dir: Path
    ) -> None:
        """A chained variable loads and reports its final root, rather than being called unresolvable.

        `libraries_dir` names OUTER, whose value names INNER. Both are set, so there is a real answer.
        Expansion runs to a fixed point and the string that gets validated is the string that gets
        returned, so a reference that only surfaces partway through expansion is never reported as an
        unset variable for a project that is correctly configured.
        """
        monkeypatch.setenv(CHAIN_INNER_ENV_VAR, str(studio_dir))
        monkeypatch.setenv(CHAIN_OUTER_ENV_VAR, f"${{{CHAIN_INNER_ENV_VAR}}}/projects")
        chained_path = write_project(tmp_path / "chained", CHAINED_LIBRARIES_YAML)

        assert isinstance(self.load(engine, chained_path), LoadProjectTemplateResultSuccess)
        info = self.loaded_by_path(self.listing(engine))[str(chained_path)]

        libraries_root = self.libraries_root_of(info)
        assert libraries_root == str(canonicalize_for_identity(studio_dir / "projects" / "shared-libraries"))
        assert CHAIN_INNER_ENV_VAR not in libraries_root
        assert CHAIN_OUTER_ENV_VAR not in libraries_root

    def test_a_reference_cycle_loads_flawed_as_a_cycle_not_as_a_missing_value(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Mutually referencing variables load FLAWED, and say so as a cycle.

        Both variables ARE set, so "no value is set for A" would be false. Expansion cannot finish,
        which is a different problem with a different fix, and the artist needs to be told which.
        The project stays loadable (so the field can be fixed in the app) with no libraries root
        reported for it.
        """
        monkeypatch.setenv(CYCLE_A_ENV_VAR, f"${{{CYCLE_B_ENV_VAR}}}")
        monkeypatch.setenv(CYCLE_B_ENV_VAR, f"${{{CYCLE_A_ENV_VAR}}}")
        cyclic_path = write_project(tmp_path / "cyclic", CYCLIC_LIBRARIES_YAML)

        assert isinstance(self.load(engine, cyclic_path), LoadProjectTemplateResultSuccess)
        listing = self.listing(engine)

        assert listing.failed_to_load == []
        info = self.loaded_by_path(listing)[str(cyclic_path)]
        assert info.libraries_root is None

        problems = [p for p in info.validation.problems if p.field_path == "libraries_dir"]
        assert len(problems) == 1
        assert "cycle" in problems[0].message or "each other" in problems[0].message
        assert "no value is set" not in problems[0].message
        assert problems[0].line_number is not None

    def test_child_reports_the_libraries_root_it_inherits_from_its_parent(self, engine: Engine, tmp_path: Path) -> None:
        """libraries_dir inherits down the chain, anchored to the DECLARING project's directory.

        This is the case the GUI cannot work out for itself, and the reason #5204 was filed: the child
        declares nothing, so its root is only knowable by walking to the ancestor that does.
        """
        base_dir = tmp_path / "base"
        base_path = write_project(base_dir, DECLARED_LIBRARIES_YAML)
        child_path = write_project(base_dir / "shots" / "sc010", CHILD_OF_BASE_YAML)

        assert isinstance(self.load(engine, base_path), LoadProjectTemplateResultSuccess)
        assert isinstance(self.load(engine, child_path), LoadProjectTemplateResultSuccess)
        by_path = self.loaded_by_path(self.listing(engine))

        child_libraries_root = self.libraries_root_of(by_path[str(child_path)])
        assert child_libraries_root == self.libraries_root_of(by_path[str(base_path)])
        assert str(child_path.parent) not in child_libraries_root

    def test_nothing_declared_falls_back_to_the_workspace_default(
        self, engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        """A project declaring no paths at all lands on the global workspace and its libraries dir."""
        solo_path = write_project(tmp_path / "solo", DECLARES_NOTHING_YAML)

        assert isinstance(self.load(engine, solo_path), LoadProjectTemplateResultSuccess)
        info = self.loaded_by_path(self.listing(engine))[str(solo_path)]

        assert info.workspace_dir == str(canonicalize_for_identity(workspace))
        assert info.libraries_root == str(canonicalize_for_identity(workspace / "libraries"))

    def test_provisioning_probes_the_roots_the_listing_reported(self, engine: Engine, tmp_path: Path) -> None:
        """The reported root is the one provisioning actually installs into, on BOTH rungs.

        PreviewProjectProvisioningResultSuccess carries no path, but its installed-version probe reads
        one, so `kind` is an observable proxy for the directory it looked in. The two projects are
        mutually discriminating: each pins the version planted at ITS OWN root, so both must answer
        SKIP. Swap the roots and both would answer OVERWRITE -- the child would find 9.9.9 against a
        ==1.2.3 pin, the solo project 1.2.3 against a ==9.9.9 pin.
        """
        base_dir = tmp_path / "base"
        base_path = write_project(base_dir, DECLARED_LIBRARIES_YAML)
        child_dir = base_dir / "shots" / "sc010"
        child_path = write_project(child_dir, CHILD_OF_BASE_YAML)
        solo_dir = tmp_path / "solo"
        solo_path = write_project(solo_dir, DECLARES_NOTHING_YAML)
        write_pinned_download(child_dir, f"=={VERSION_AT_DECLARED_ROOT}")
        write_pinned_download(solo_dir, f"=={VERSION_AT_WORKSPACE_DEFAULT}")

        for project_path in (base_path, child_path, solo_path):
            assert isinstance(self.load(engine, project_path), LoadProjectTemplateResultSuccess)
        by_path = self.loaded_by_path(self.listing(engine))
        child_info = by_path[str(child_path)]
        solo_info = by_path[str(solo_path)]

        # Inherited-declaration rung for the child, workspace-default rung for the solo project.
        child_libraries_root = self.libraries_root_of(child_info)
        solo_libraries_root = self.libraries_root_of(solo_info)
        assert child_libraries_root != solo_libraries_root
        plant_installed_library(Path(child_libraries_root), VERSION_AT_DECLARED_ROOT)
        plant_installed_library(Path(solo_libraries_root), VERSION_AT_WORKSPACE_DEFAULT)

        for info, expected_version in (
            (child_info, VERSION_AT_DECLARED_ROOT),
            (solo_info, VERSION_AT_WORKSPACE_DEFAULT),
        ):
            preview = engine.handle_request(PreviewProjectProvisioningRequest(project_id=info.project_id))
            assert isinstance(preview, PreviewProjectProvisioningResultSuccess), preview.result_details
            assert len(preview.actions) == 1, preview.actions
            action = preview.actions[0]
            assert action.installed_version == expected_version
            assert action.kind is LibraryProvisioningActionKind.SKIP

    def test_provisioning_declines_to_preview_a_project_that_is_not_loaded(self, engine: Engine) -> None:
        """The preview refuses rather than previewing against the current project's config layers.

        Its dirs come from the loaded-template registry, so an id that is not there has no project dir
        and no workspace to merge -- answering anyway would describe some other project's libraries.
        """
        preview = engine.handle_request(PreviewProjectProvisioningRequest(project_id="no-such-project"))

        assert isinstance(preview, PreviewProjectProvisioningResultFailure)
        assert "not loaded" in str(preview.result_details)

    def test_provisioning_declines_to_preview_a_flawed_project(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The preview mirrors the activation gate rather than planning against a fallback root.

        A FLAWED project's activation will be refused, so a plan computed from the workspace-default
        libraries root would show the user changes that can never be applied.
        """
        monkeypatch.delenv(LIBS_ENV_VAR, raising=False)
        base_path = write_project(tmp_path / "base", DECLARED_LIBRARIES_YAML)
        load_result = self.load(engine, base_path)
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        preview = engine.handle_request(PreviewProjectProvisioningRequest(project_id=load_result.project_id))

        assert isinstance(preview, PreviewProjectProvisioningResultFailure)
        assert "declared paths cannot be resolved" in str(preview.result_details)
        assert LIBS_ENV_VAR in str(preview.result_details)

    @pytest.mark.asyncio
    async def test_resolver_answers_nothing_for_an_id_that_is_not_a_loadable_project(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """The public resolver returns None rather than guessing a root for an id it cannot read.

        Covers the two ways an id fails to produce a template: no such project, and a path that exists
        but is not a usable project file. Both must decline instead of falling back to the workspace
        default, or a caller asking about a project it does not have would be told a plausible path.
        """
        unparsable = tmp_path / "junk" / PROJECT_FILE_NAME
        unparsable.parent.mkdir(parents=True, exist_ok=True)
        unparsable.write_text("name: [unclosed\n", encoding="utf-8")

        assert await engine.project_manager.resolve_libraries_root_for_project_id("no-such-project") is None
        assert await engine.project_manager.resolve_libraries_root_for_project_id(str(unparsable)) is None

    def test_unresolvable_declaration_loads_flawed_with_the_field_and_its_line(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With the variable unset the project loads FLAWED instead of substituting a path.

        The listing reports it as loaded -- so the GUI can open it and the bad value can be fixed in
        the app -- but with no libraries root, so "here is where the libraries live" stays distinct
        from "this project is broken." The workspace, which the project does not declare, still
        resolves normally.
        """
        monkeypatch.delenv(LIBS_ENV_VAR, raising=False)
        base_path = write_project(tmp_path / "base", DECLARED_LIBRARIES_YAML)

        assert isinstance(self.load(engine, base_path), LoadProjectTemplateResultSuccess)
        listing = self.listing(engine)

        assert listing.failed_to_load == []
        info = self.loaded_by_path(listing)[str(base_path)]
        assert info.workspace_dir is not None
        assert info.libraries_root is None

        problems = [p for p in info.validation.problems if p.field_path == "libraries_dir"]
        assert len(problems) == 1
        assert LIBS_ENV_VAR in problems[0].message
        # The scalar's line is tracked from its key's position in the parent mapping; before that,
        # every scalar field in a project file reported no line at all.
        assert problems[0].line_number is not None
        cited_line = base_path.read_text(encoding="utf-8").splitlines()[problems[0].line_number - 1]
        assert "libraries_dir" in cited_line

    def test_activation_refuses_a_flawed_project_end_to_end(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The whole point of loading FLAWED: the project opens, but switching to it is refused.

        Drives SetCurrentProjectRequest through the real event system so the gate, not a mock, is
        what says no. The failure names the field's problem so the GUI can point at it.
        """
        from griptape_nodes.retained_mode.events.project_events import (
            SetCurrentProjectRequest,
            SetCurrentProjectResultFailure,
        )
        from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

        monkeypatch.delenv(LIBS_ENV_VAR, raising=False)
        base_path = write_project(tmp_path / "base", DECLARED_LIBRARIES_YAML)
        load_result = self.load(engine, base_path)
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)

        result = engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        assert isinstance(result, SetCurrentProjectResultFailure)
        assert "declared paths cannot be resolved" in str(result.result_details)
        assert LIBS_ENV_VAR in str(result.result_details)
        # The refusal must not half-take: the engine stays on the project it had
        # (system defaults here), with the refused project nowhere in its state.
        assert engine.project_manager._current_project_id == SYSTEM_DEFAULTS_KEY

    def test_a_broken_parent_blocks_its_child_but_not_an_unrelated_project(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path
    ) -> None:
        """A FLAWED base no longer bricks its family, but nothing inherits around it either.

        The base and its child both LOAD (so the family stays visible and the base's value can be
        fixed in the app), and neither reports a libraries root: the base's declaration is
        unresolvable and the child's inheritance is blocked on it, never quietly replaced with the
        workspace default. A project that never referenced the variable is untouched.
        """
        monkeypatch.delenv(LIBS_ENV_VAR, raising=False)
        base_dir = tmp_path / "base"
        base_path = write_project(base_dir, DECLARED_LIBRARIES_YAML)
        child_path = write_project(base_dir / "shots" / "sc010", CHILD_OF_BASE_YAML)
        solo_path = write_project(tmp_path / "solo", DECLARES_NOTHING_YAML)

        assert isinstance(self.load(engine, base_path), LoadProjectTemplateResultSuccess)
        assert isinstance(self.load(engine, child_path), LoadProjectTemplateResultSuccess)
        assert isinstance(self.load(engine, solo_path), LoadProjectTemplateResultSuccess)
        listing = self.listing(engine)

        assert listing.failed_to_load == []
        by_path = self.loaded_by_path(listing)
        assert by_path[str(base_path)].libraries_root is None
        assert by_path[str(child_path)].libraries_root is None

        solo_info = by_path[str(solo_path)]
        assert solo_info.libraries_root == str(canonicalize_for_identity(workspace / "libraries"))
