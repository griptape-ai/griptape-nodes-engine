"""A library's dependency install says what it is installing, in the log and on its progress event."""

import logging
import subprocess
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.app_events import (
    EngineInitializationProgress,
    InitializationPhase,
    InitializationStatus,
)
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileResultFailure
from griptape_nodes.retained_mode.managers.library.common import LibraryLoadProgress
from griptape_nodes.retained_mode.managers.library.dependencies import (
    DependencyInstallError,
    summarize_dependencies,
)
from griptape_nodes.retained_mode.managers.library.environment import LibraryVenvInitResult

_LIBRARY_FILE = "/libraries/diffusers/griptape_nodes_library.json"
_DEPENDENCIES = ["torch>=2.4,<3", "diffusers==0.33.0", "transformers", "accelerate", "safetensors"]


def _config_value(key: str, **_: object) -> object:
    if key == "log_level":
        return "INFO"
    if key == "minimum_disk_space_gb_libraries":
        return 5.0
    return None


class _Install:
    """Runs one dependency-set install with the venv and uv faked, recording what was reported."""

    def __init__(self, engine: Engine, *, reused: bool, uv_failures: int = 0) -> None:
        self.engine = engine
        self.reused = reused
        # How many uv runs fail before one succeeds, to drive the corrupt-venv rebuild.
        self.uv_failures = uv_failures
        self.progress_events: list[EngineInitializationProgress] = []
        # Progress events already sent when uv started, to show the report comes before the wait.
        self.progress_events_when_uv_started: list[EngineInitializationProgress] | None = None

    def record_event(self, event: object) -> None:
        if isinstance(event, AppEvent) and isinstance(event.payload, EngineInitializationProgress):
            self.progress_events.append(event.payload)

    async def fake_uv(self, *_: object, **__: object) -> MagicMock:
        if self.progress_events_when_uv_started is None:
            self.progress_events_when_uv_started = list(self.progress_events)
        if self.uv_failures > 0:
            self.uv_failures -= 1
            raise subprocess.CalledProcessError(returncode=2, cmd="uv")
        return MagicMock(returncode=0)

    async def run(self, pip_dependencies: list[str], *, execution: bool = False) -> None:
        dependencies = self.engine.library_manager.dependencies
        environment = self.engine.library_manager.environment
        with (
            patch.object(self.engine.event_manager, "put_event", side_effect=self.record_event),
            patch.object(environment, "get_library_venv_path", return_value=Path("nonexistent-library-venv")),
            patch.object(
                environment,
                "init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=self.reused),
            ),
            patch.object(environment, "can_write_to_venv_location", return_value=True),
            patch.object(
                dependencies, "_reset_and_init_library_venv", new_callable=AsyncMock, return_value=MagicMock()
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library.dependencies.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library.dependencies.subprocess_run",
                side_effect=self.fake_uv,
            ),
            patch.object(self.engine.config_manager, "get_config_value", side_effect=_config_value),
        ):
            await dependencies._install_dependency_set(
                library_name="Diffusers",
                library_file_path=_LIBRARY_FILE,
                pip_dependencies=pip_dependencies,
                pip_install_flags=[],
                execution=execution,
            )


@pytest.fixture
def during_load(engine: Engine) -> Generator[None, None, None]:
    """Run the test as if a load were registering the library, third of seven."""
    with engine.library_manager.track_load_progress(_LIBRARY_FILE, current=3, total=7):
        yield


class TestAFreshEnvironmentInstall:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_reports_the_packages_before_uv_runs(self, engine: Engine) -> None:
        install = _Install(engine, reused=False)

        await install.run(_DEPENDENCIES)

        assert install.progress_events_when_uv_started == install.progress_events[:1]
        assert install.progress_events[:1] == [
            EngineInitializationProgress(
                phase=InitializationPhase.LIBRARIES,
                item_name="Diffusers",
                status=InitializationStatus.LOADING,
                current=3,
                total=7,
                is_worker=False,
                detail=(
                    "Installing 5 packages for Diffusers: torch, diffusers, transformers, and 2 more. "
                    "The first install can take several minutes."
                ),
                dependencies=_DEPENDENCIES,
            )
        ]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_logs_the_start_and_how_long_it_took(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await _Install(engine, reused=False).run(_DEPENDENCIES)

        info_messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
        assert (
            "Installing 5 packages for library 'Diffusers' (edit-time environment): "
            "torch, diffusers, transformers, and 2 more"
        ) in info_messages
        assert any(
            message.startswith("Installed packages for library 'Diffusers' (edit-time environment) in ")
            for message in info_messages
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_a_single_package_is_not_pluralized(self, engine: Engine) -> None:
        install = _Install(engine, reused=False)

        await install.run(["torch"])

        assert install.progress_events[0].detail == (
            "Installing 1 package for Diffusers: torch. The first install can take several minutes."
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_an_execution_install_names_its_environment(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await _Install(engine, reused=False).run(_DEPENDENCIES, execution=True)

        assert any("(execution environment)" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_clears_the_detail_once_the_install_finishes(self, engine: Engine) -> None:
        install = _Install(engine, reused=False)

        await install.run(_DEPENDENCIES)

        assert [(event.detail, event.dependencies) for event in install.progress_events[1:]] == [(None, None)]
        assert install.progress_events[-1].status is InitializationStatus.LOADING

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_clears_the_detail_when_the_install_fails(self, engine: Engine) -> None:
        install = _Install(engine, reused=False, uv_failures=2)

        with pytest.raises(DependencyInstallError):
            await install.run(_DEPENDENCIES)

        assert [event.detail is None for event in install.progress_events] == [False, True]


class TestAReusedEnvironmentInstall:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_is_reported_as_a_check(self, engine: Engine) -> None:
        install = _Install(engine, reused=True)

        await install.run(_DEPENDENCIES)

        assert [event.detail for event in install.progress_events] == [
            "Checking packages for Diffusers. Installing any that are new can take several minutes.",
            None,
        ]
        assert install.progress_events[0].dependencies == _DEPENDENCIES

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_logs_nothing_at_info(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await _Install(engine, reused=True).run(_DEPENDENCIES)

        assert [record for record in caplog.records if record.levelno >= logging.INFO] == []

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_a_slow_install_logs_its_duration_at_info(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The install starts at 100 s and finishes at 400 s on the faked clock.
        fake_time = MagicMock()
        fake_time.monotonic.side_effect = [100.0, 400.0]
        with (
            patch("griptape_nodes.retained_mode.managers.library.dependencies.time", fake_time),
            caplog.at_level(logging.INFO, logger="griptape_nodes"),
        ):
            await _Install(engine, reused=True).run(_DEPENDENCIES)

        info_messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
        assert info_messages == ["Installed packages for library 'Diffusers' (edit-time environment) in 300.0 s"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_a_quick_rebuild_still_logs_its_completion_at_info(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await _Install(engine, reused=True, uv_failures=2).run(_DEPENDENCIES)

        assert any(
            record.levelno == logging.INFO and record.getMessage().startswith("Installed packages for library")
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_a_rebuild_announces_a_full_install(self, engine: Engine) -> None:
        install = _Install(engine, reused=True, uv_failures=2)

        await install.run(_DEPENDENCIES)

        details = [event.detail for event in install.progress_events]
        assert details == [
            "Checking packages for Diffusers. Installing any that are new can take several minutes.",
            (
                "Installing 5 packages for Diffusers: torch, diffusers, transformers, and 2 more. "
                "The first install can take several minutes."
            ),
            None,
        ]


class TestAnInstallOutsideALoad:
    @pytest.mark.asyncio
    async def test_logs_without_sending_a_progress_event(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        install = _Install(engine, reused=False)

        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await install.run(_DEPENDENCIES)

        assert install.progress_events == []
        assert any(record.getMessage().startswith("Installing 5 packages") for record in caplog.records)


class TestNothingToInstall:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("during_load")
    async def test_sends_no_progress_event(self, engine: Engine) -> None:
        install = _Install(engine, reused=False)

        await install.run([])

        assert install.progress_events == []


class TestSummarizeDependencies:
    @pytest.mark.parametrize(
        ("pip_dependencies", "expected"),
        [
            (["torch"], "torch"),
            (["torch", "diffusers", "transformers"], "torch, diffusers, transformers"),
            (["torch", "diffusers", "transformers", "accelerate"], "torch, diffusers, transformers, and 1 more"),
            (["torch>=2.4,<3", "diffusers[torch]==0.33.0"], "torch, diffusers"),
            (["git+https://github.com/example/package.git"], "git+https://github.com/example/package.git"),
        ],
    )
    def test_names_the_first_packages_and_counts_the_rest(self, pip_dependencies: list[str], expected: str) -> None:
        assert summarize_dependencies(pip_dependencies) == expected


class TestLoadProgressTracking:
    def test_a_different_spelling_of_the_same_path_finds_the_progress(self, engine: Engine, tmp_path: Path) -> None:
        library_file = tmp_path / "library" / "griptape_nodes_library.json"
        roundabout_spelling = str(tmp_path / "library" / ".." / "library" / "griptape_nodes_library.json")

        with engine.library_manager.track_load_progress(roundabout_spelling, current=1, total=2):
            assert engine.library_manager.load_progress_for(str(library_file)) == LibraryLoadProgress(
                current=1, total=2
            )

    def test_the_progress_is_cleared_when_the_load_raises(self, engine: Engine) -> None:
        library_manager = engine.library_manager

        def failing_load() -> None:
            with library_manager.track_load_progress(_LIBRARY_FILE, current=1, total=1):
                raise RuntimeError

        with pytest.raises(RuntimeError):
            failing_load()

        assert library_manager.load_progress_for(_LIBRARY_FILE) is None

    def test_a_nested_load_of_the_same_library_restores_the_outer_progress(self, engine: Engine) -> None:
        library_manager = engine.library_manager

        with library_manager.track_load_progress(_LIBRARY_FILE, current=2, total=5):
            with library_manager.track_load_progress(_LIBRARY_FILE, current=1, total=1):
                pass
            assert library_manager.load_progress_for(_LIBRARY_FILE) == LibraryLoadProgress(current=2, total=5)

        assert library_manager.load_progress_for(_LIBRARY_FILE) is None

    def test_overlapping_loads_can_finish_in_either_order(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        startup_load = library_manager.track_load_progress(_LIBRARY_FILE, current=2, total=5)
        reload = library_manager.track_load_progress(_LIBRARY_FILE, current=1, total=1)

        startup_load.__enter__()
        reload.__enter__()
        startup_load.__exit__(None, None, None)
        progress_while_reload_runs = library_manager.load_progress_for(_LIBRARY_FILE)
        reload.__exit__(None, None, None)

        assert progress_while_reload_runs == LibraryLoadProgress(current=1, total=1)
        assert library_manager.load_progress_for(_LIBRARY_FILE) is None

    @pytest.mark.asyncio
    async def test_a_failed_registration_at_startup_clears_the_progress(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        progress_during_registration: list[LibraryLoadProgress | None] = []

        async def failed_registration(_request: object) -> RegisterLibraryFromFileResultFailure:
            progress_during_registration.append(library_manager.load_progress_for(_LIBRARY_FILE))
            return RegisterLibraryFromFileResultFailure(result_details="broken library")

        with (
            patch.object(library_manager.registration, "register_library_from_file_request", failed_registration),
            patch.object(engine.event_manager, "put_event"),
        ):
            await library_manager._load_and_track_library(_LIBRARY_FILE, index=2, total=4)

        assert progress_during_registration == [LibraryLoadProgress(current=2, total=4)]
        assert library_manager.load_progress_for(_LIBRARY_FILE) is None
