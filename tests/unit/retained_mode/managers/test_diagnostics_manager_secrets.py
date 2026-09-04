"""Tests for the secrets section of the diagnostics report.

Two separate promises are kept here.

The first is a hard guarantee: this section reports *which* secrets exist and *where*,
never what they are. Values are read, because there is no way to know whether a key is
set without reading it, but every one is reduced to a boolean before it can reach the
report. `test_no_secret_value_reaches_the_report` is the assertion that matters.

The second is usefulness. "My API key is set, so why does it say unauthorized?" is
usually shadowing: a stale key exported in a shell, or a key blanked in the workspace
`.env` covering a working one in the global file. So the section reports every source a
key was found in, in the order the engine searches them, and which one actually won.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from griptape_nodes.retained_mode.managers import diagnostics_manager as diagnostics_manager_module
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager
from griptape_nodes.retained_mode.managers.settings import SECRETS_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.common.diagnostics.report import SecretDiagnostics

# The name of a secret, which is exactly what this section is allowed to report.
_SECRET_NAME = "GTN_TEST_DIAGNOSTICS_KEY"  # noqa: S105
_OTHER_NAME = "GTN_TEST_DIAGNOSTICS_OTHER"

_OS_VALUE = "value-from-the-shell"
_WORKSPACE_VALUE = "value-from-the-workspace-file"
_GLOBAL_VALUE = "value-from-the-global-file"

_ENVIRONMENT = "environment variable"
_WORKSPACE = "workspace .env"
_GLOBAL = "global .env"


def _config_reader(values: dict[str, object]) -> Callable[..., object]:
    """Return a ``get_config_value`` stand-in that only answers the keys it was given.

    Any other key raises, so a section that starts reading a second setting is caught here
    rather than being handed whatever this test happened to set up for the first one.
    """

    def read(key: str, **_kwargs: object) -> object:
        if key not in values:
            msg = f"the secrets section read config key '{key}', which this test does not define"
            raise AssertionError(msg)
        return values[key]

    return read


class _ExplodingSecretsManager:
    """A secrets manager whose workspace cannot be resolved."""

    @property
    def workspace_env_path(self) -> Path:
        msg = "the workspace directory could not be resolved"
        raise OSError(msg)


class _Layout:
    """The three places the engine looks for a secret, wired up for one test."""

    def __init__(self, manager: DiagnosticsManager, engine: Mock) -> None:
        self.manager = manager
        # Kept as the Mock rather than reached through `manager.engine`, which is typed as
        # a real Engine and so cannot be reconfigured mid-test.
        self.engine = engine

    def declare(self, *names: str) -> None:
        """Say which secrets the config asks the engine to register.

        Answered for that one key rather than by setting a blanket ``return_value``, which
        would hand this dict to every config read the section makes and make a test pass
        for reading the wrong setting.
        """
        self.engine.config_manager.get_config_value.side_effect = _config_reader(
            {SECRETS_TO_REGISTER_KEY: dict.fromkeys(names, "")}
        )

    def entries(self) -> list[SecretDiagnostics]:
        return self.manager._build_secrets_section([])

    def entry(self, name: str = _SECRET_NAME) -> SecretDiagnostics:
        matches = [entry for entry in self.entries() if entry.name == name]
        assert len(matches) == 1, f"expected exactly one entry for {name}, got {matches}"
        return matches[0]


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Layout:
    """A manager whose three secret sources are all empty files under tmp_path.

    Each test fills in only the sources it cares about, so a test that reports the wrong
    source fails rather than accidentally agreeing with the machine it runs on.
    """
    global_env = tmp_path / "global.env"
    workspace_env = tmp_path / "workspace.env"
    monkeypatch.setattr(diagnostics_manager_module, "ENV_VAR_PATH", global_env)
    monkeypatch.delenv(_SECRET_NAME, raising=False)
    monkeypatch.delenv(_OTHER_NAME, raising=False)

    engine = Mock()
    engine.config_manager.get_config_value.side_effect = _config_reader({SECRETS_TO_REGISTER_KEY: {}})
    engine.secrets_manager.workspace_env_path = workspace_env

    manager = DiagnosticsManager(Mock(), engine=engine)
    return _Layout(manager, engine)


def _write_env(path: Path, **values: str) -> None:
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")


class TestPrecedence:
    def test_a_secret_found_only_in_the_global_file(self, layout: _Layout, tmp_path: Path) -> None:
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})

        entry = layout.entry()

        assert entry.is_set is True
        assert entry.effective_source == _GLOBAL
        assert entry.sources == [_GLOBAL]

    def test_the_workspace_file_beats_the_global_one(self, layout: _Layout, tmp_path: Path) -> None:
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        _write_env(tmp_path / "workspace.env", **{_SECRET_NAME: _WORKSPACE_VALUE})

        entry = layout.entry()

        assert entry.effective_source == _WORKSPACE
        # Both are listed, highest priority first, so the reader can see the one that lost.
        assert entry.sources == [_WORKSPACE, _GLOBAL]

    def test_an_environment_variable_beats_both_files(
        self, layout: _Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        _write_env(tmp_path / "workspace.env", **{_SECRET_NAME: _WORKSPACE_VALUE})
        monkeypatch.setenv(_SECRET_NAME, _OS_VALUE)

        entry = layout.entry()

        assert entry.effective_source == _ENVIRONMENT
        assert entry.sources == [_ENVIRONMENT, _WORKSPACE, _GLOBAL]

    def test_sources_are_listed_in_the_order_the_engine_searches_them(
        self, layout: _Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The list is read top to bottom as "this is what won, and this is what it beat"."""
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        monkeypatch.setenv(_SECRET_NAME, _OS_VALUE)

        entry = layout.entry()

        assert entry.sources == [_ENVIRONMENT, _GLOBAL]


class TestShadowing:
    def test_an_environment_variable_matching_the_files_is_not_a_separate_source(
        self, layout: _Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An environment variable equal to the file value is not reported separately.

        The engine copies every `.env` entry into the environment at startup, so mere
        presence there means nothing. Reporting it would put "environment variable" on
        every key and bury the one case that matters.
        """
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        monkeypatch.setenv(_SECRET_NAME, _GLOBAL_VALUE)

        entry = layout.entry()

        assert entry.sources == [_GLOBAL]
        assert entry.effective_source == _GLOBAL

    def test_an_environment_variable_differing_from_the_files_is_reported(
        self, layout: _Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An environment variable that disagrees with the files is reported and wins.

        This is the whole reason the comparison exists: a stale shell export beating the
        `.env` file the user has been editing, which is invisible from the file alone.
        """
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        monkeypatch.setenv(_SECRET_NAME, "something-else-entirely")

        entry = layout.entry()

        assert entry.effective_source == _ENVIRONMENT

    def test_a_key_blanked_in_the_workspace_file_shadows_a_working_global_one(
        self, layout: _Layout, tmp_path: Path
    ) -> None:
        """Reported as not set, because it is not: the empty workspace value wins outright.

        Saying "set, in the global file" here would send someone to look at a file whose
        value is never used.
        """
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        _write_env(tmp_path / "workspace.env", **{_SECRET_NAME: ""})

        entry = layout.entry()

        assert entry.is_set is False
        assert entry.effective_source is None
        # Still listed under both, so the global value is visible as the thing being shadowed.
        assert entry.sources == [_WORKSPACE, _GLOBAL]

    def test_a_bare_name_with_no_value_counts_as_declared_but_empty(self, layout: _Layout, tmp_path: Path) -> None:
        """`FOO` on a line of its own is how a `.env` reads when someone deleted the value."""
        (tmp_path / "global.env").write_text(f"{_SECRET_NAME}\n", encoding="utf-8")

        entry = layout.entry()

        assert entry.is_set is False
        assert entry.sources == [_GLOBAL]


class TestDeclaredSecrets:
    def test_a_declared_secret_that_was_never_filled_in_is_still_listed(self, layout: _Layout) -> None:
        """Usually the answer to "why does this library say no key".

        A key missing from the list entirely would look like a bug in the report rather
        than a key nobody has set.
        """
        layout.declare(_SECRET_NAME)

        entry = layout.entry()

        assert entry.is_set is False
        assert entry.sources == []
        assert entry.effective_source is None
        assert entry.declared_in_config is True

    def test_a_secret_set_but_not_declared_is_marked_as_undeclared(self, layout: _Layout, tmp_path: Path) -> None:
        """A key the libraries never ask for: set, but nothing will read it."""
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})

        entry = layout.entry()

        assert entry.is_set is True
        assert entry.declared_in_config is False

    def test_a_declared_secret_that_is_set_is_marked_as_both(self, layout: _Layout, tmp_path: Path) -> None:
        layout.declare(_SECRET_NAME)
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})

        entry = layout.entry()

        assert entry.is_set is True
        assert entry.declared_in_config is True

    def test_a_declared_secret_set_only_in_the_environment_is_reported(
        self, layout: _Layout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing in any file, so the key is only a candidate because the config declared it."""
        layout.declare(_SECRET_NAME)
        monkeypatch.setenv(_SECRET_NAME, _OS_VALUE)

        entry = layout.entry()

        assert entry.is_set is True
        assert entry.effective_source == _ENVIRONMENT

    def test_entries_are_sorted_by_name(self, layout: _Layout, tmp_path: Path) -> None:
        layout.declare(_OTHER_NAME)
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})

        names = [entry.name for entry in layout.entries()]

        assert names == sorted(names)
        assert {_SECRET_NAME, _OTHER_NAME} <= set(names)


class TestNoValuesEscape:
    def test_no_secret_value_reaches_the_report(
        self, layout: _Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guarantee behind this whole section, checked against the serialized output.

        Every source is filled with a different value so a leak from any one of them
        fails this test rather than the other two masking it.
        """
        layout.declare(_SECRET_NAME)
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        _write_env(tmp_path / "workspace.env", **{_SECRET_NAME: _WORKSPACE_VALUE})
        monkeypatch.setenv(_SECRET_NAME, _OS_VALUE)

        entries = layout.entries()

        serialized = json.dumps([entry.model_dump() for entry in entries])
        assert _OS_VALUE not in serialized
        assert _WORKSPACE_VALUE not in serialized
        assert _GLOBAL_VALUE not in serialized
        # The name is the diagnostic signal and is not a secret, so it has to survive.
        assert _SECRET_NAME in serialized


class TestUnreadableSources:
    def test_a_missing_env_file_is_not_an_error(self, layout: _Layout) -> None:
        """Neither file has to exist; plenty of engines are configured entirely by environment."""
        assert layout.entries() == []

    def test_an_unresolvable_workspace_path_is_reported_and_the_rest_still_collected(
        self, layout: _Layout, tmp_path: Path
    ) -> None:
        """A workspace that cannot be resolved is often why a report is being collected."""
        _write_env(tmp_path / "global.env", **{_SECRET_NAME: _GLOBAL_VALUE})
        layout.engine.secrets_manager = _ExplodingSecretsManager()
        warnings: list[str] = []

        entries = layout.manager._build_secrets_section(warnings)

        assert [entry.name for entry in entries] == [_SECRET_NAME]
        assert any("workspace .env path" in warning for warning in warnings)

    def test_an_env_file_that_is_not_utf8_is_reported_and_the_rest_still_collected(
        self, layout: _Layout, tmp_path: Path
    ) -> None:
        """A hand-edited `.env` saved in another encoding is a real state, and not an `OSError`.

        `dotenv` decodes as UTF-8, so it raises `UnicodeDecodeError` -- a `ValueError`. Caught
        as only `OSError`, one badly saved file took the whole collection down.
        """
        (tmp_path / "global.env").write_bytes(b"GTN_TEST_LATIN1=a-value-\xe9\n")
        _write_env(tmp_path / "workspace.env", **{_SECRET_NAME: _WORKSPACE_VALUE})
        warnings: list[str] = []

        entries = layout.manager._build_secrets_section(warnings)

        assert [entry.name for entry in entries] == [_SECRET_NAME]
        assert any("global.env" in warning for warning in warnings)


class TestKnownSecretValues:
    def test_returns_the_values_so_they_can_be_scrubbed_from_logs(self, layout: _Layout) -> None:
        """Only non-empty values, so an unset key does not become a match-everything pattern.

        The engine is the only thing that knows these, which makes it the only thing that
        can find one a library logged verbatim.
        """
        layout.engine.secrets_manager._read_merged_env_files.return_value = {
            _SECRET_NAME: _GLOBAL_VALUE,
            _OTHER_NAME: "",
        }

        values = layout.manager._known_secret_values()

        assert values == [_GLOBAL_VALUE]

    def test_finds_a_secret_that_only_ever_existed_as_an_environment_variable(
        self, layout: _Layout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key exported in a shell is in no `.env` file, and is still a key a library can log.

        Headless engines and containers are configured exactly this way, so a scrub list
        built from the files alone left those engines' keys in their own bundled logs.
        """
        layout.declare(_SECRET_NAME)
        layout.engine.secrets_manager._read_merged_env_files.return_value = {}
        monkeypatch.setenv(_SECRET_NAME, _OS_VALUE)

        assert layout.manager._known_secret_values() == [_OS_VALUE]

    def test_reads_only_the_names_there_is_a_reason_to_expect(
        self, layout: _Layout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded to declared and file-known names rather than sweeping the environment.

        `PATH` and `HOME` are not secrets, and turning every environment value into a search
        pattern would replace most of a bundle with `<redacted>`.
        """
        layout.declare(_SECRET_NAME)
        layout.engine.secrets_manager._read_merged_env_files.return_value = {}
        monkeypatch.setenv(_SECRET_NAME, _OS_VALUE)
        monkeypatch.setenv("GTN_TEST_DIAGNOSTICS_UNDECLARED", "an-unrelated-value")

        assert layout.manager._known_secret_values() == [_OS_VALUE]

    def test_a_value_in_both_a_file_and_the_environment_is_listed_once(
        self, layout: _Layout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engine copies `.env` entries into the environment, so most values are found twice."""
        layout.engine.secrets_manager._read_merged_env_files.return_value = {_SECRET_NAME: _GLOBAL_VALUE}
        monkeypatch.setenv(_SECRET_NAME, _GLOBAL_VALUE)

        assert layout.manager._known_secret_values() == [_GLOBAL_VALUE]

    def test_an_unreadable_env_file_costs_thoroughness_not_the_report(self, layout: _Layout) -> None:
        """Pattern-based redaction still applies, so this degrades rather than fails."""
        layout.engine.secrets_manager._read_merged_env_files.side_effect = OSError("permission denied")

        assert layout.manager._known_secret_values() == []

    def test_an_env_file_that_is_not_utf8_costs_thoroughness_not_the_report(self, layout: _Layout) -> None:
        """`dotenv` decodes as UTF-8, so a file in another encoding raises a `ValueError`, not an `OSError`."""
        layout.engine.secrets_manager._read_merged_env_files.side_effect = UnicodeDecodeError(
            "utf-8", b"a-value-\xe9", 8, 9, "invalid continuation byte"
        )

        assert layout.manager._known_secret_values() == []

    def test_a_declared_name_with_no_value_anywhere_adds_no_pattern(self, layout: _Layout) -> None:
        """An empty value as a search pattern would match everywhere and redact the whole bundle."""
        layout.declare(_SECRET_NAME)
        layout.engine.secrets_manager._read_merged_env_files.return_value = {_SECRET_NAME: ""}

        assert layout.manager._known_secret_values() == []
