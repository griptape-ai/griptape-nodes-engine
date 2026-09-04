"""The one promise a diagnostics bundle cannot break: no secret value is in it.

Everything else about a bundle is a convenience. A bundle exists to be attached to a bug
report and handed to somebody else, so a live API key inside one is a credential disclosed
to whoever that turns out to be. Every other test in this area checks one redaction rule
against one string; these assemble the finished artifact with the real ``Redactor``, the
real ``DiagnosticsBundle``, and the report section the config actually flows through, then
look for the secret in every file the archive contains.

Read as members, never as raw zip bytes: the archive is deflated, so a plaintext key in a
member is not plaintext in the bytes, and a search over the bytes would report a clean
bundle for a leaking one.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from griptape_nodes.common.diagnostics.bundle import DiagnosticsBundle
from griptape_nodes.common.diagnostics.redaction import REDACTED, RedactionReason, Redactor
from griptape_nodes.common.diagnostics.report import DiagnosticsReport, EngineDiagnostics, HostDiagnostics
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager

if TYPE_CHECKING:
    from pathlib import Path

# Long enough to be searched for in free text, and distinctive enough that finding one
# anywhere in a bundle is unambiguous rather than a coincidence of the machine it ran on.
_ENV_SECRET = "gtn-test-secret-value-9f3c2a17b4"  # noqa: S105
_WORKFLOW_SECRET = "gtn-test-secret-value-1d8e6f04c2"  # noqa: S105

_LOG_FILE_NAME = "engine-20260101-000000-1.log"


class _Bundle:
    """A finished bundle, read back as the files it contains.

    Attributes:
        members: Every file in the archive, decoded, keyed by its path inside the zip.
    """

    def __init__(self, data: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.members = {
                name: archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if not name.endswith("/")
            }

    def holding_a_secret(self) -> list[str]:
        """Return the names of any files a secret value survived into."""
        return sorted(name for name, text in self.members.items() if _ENV_SECRET in text or _WORKFLOW_SECRET in text)


@pytest.fixture
def bundle(tmp_path: Path) -> _Bundle:
    """Assemble a real bundle from sources that each hold a secret.

    A secret is planted in every place a bundle copies text from -- a settings value, a log
    file on disk, this session's in-memory log, and the open workflow's source -- so one
    assertion over the members covers all of them at once. The redactor is told the two
    values, the way the manager tells it what it read out of the ``.env`` files.
    """
    log_file = tmp_path / _LOG_FILE_NAME
    log_file.write_text(
        f"2026-01-01T00:00:00.000Z INFO - - authenticating with {_ENV_SECRET}\n",
        encoding="utf-8",
    )

    workflow_file = tmp_path / "flow.py"
    workflow_file.write_text(f'API_KEY = "{_WORKFLOW_SECRET}"\n', encoding="utf-8")

    engine = Mock()
    engine.config_manager.config_file_layers = []
    # A credential-named setting, and the same value again under a name that says nothing.
    # The first is removed for its key, the second only because the redactor knows the value.
    engine.config_manager.merged_config = {
        "nodes": {"OpenAi": {"api_key": _ENV_SECRET}},
        "last_used_endpoint": f"https://api.example.com/v1?token={_ENV_SECRET}",
    }
    manager = DiagnosticsManager(Mock(), engine=engine)

    redactor = Redactor(secret_values=[_ENV_SECRET, _WORKFLOW_SECRET])
    warnings: list[str] = []

    with DiagnosticsBundle(redactor) as staged:
        staged.add_session_log([f"2026-01-01T00:00:00.000Z INFO - - connecting with {_ENV_SECRET}"])
        staged.add_log_files([log_file], warnings)
        staged.add_workflow(workflow_file, warnings)
        staged.add_report(
            DiagnosticsReport(
                generated_at="2026-01-01T00:00:00+00:00",
                # The section the planted settings flow through, built by the real code
                # against the real redactor. The engine and host sections are required by
                # the model and hold nothing a secret can reach, so they are filled in
                # rather than gathered.
                engine=EngineDiagnostics(python_version="3.12.0", python_executable="/usr/bin/python", process_id=1),
                host=HostDiagnostics(system="Linux", release="6.0", version="#1", machine="x86_64"),
                config=manager._build_config_section(redactor),
            )
        )
        staged.add_readme()
        staged.write_manifest(
            generated_at="2026-01-01T00:00:00+00:00",
            engine_version="1.2.3",
            identity_normalized=True,
            warnings=warnings,
        )
        return _Bundle(staged.to_zip_bytes())


class TestNoSecretValueIsInABundle:
    def test_the_bundle_holds_the_files_the_secrets_were_planted_in(self, bundle: _Bundle) -> None:
        """Named first, because every assertion below would also pass for an empty archive.

        A bundle that failed to stage its logs proves nothing about having redacted them.
        """
        assert "logs/session.log" in bundle.members
        assert f"logs/{_LOG_FILE_NAME}" in bundle.members
        assert "workflow/flow.py" in bundle.members
        assert "report.json" in bundle.members

    def test_no_member_holds_a_secret_value(self, bundle: _Bundle) -> None:
        """The whole promise, over every file in the archive rather than one of them."""
        assert bundle.holding_a_secret() == []

    def test_no_member_is_named_after_a_secret(self, bundle: _Bundle) -> None:
        """Entry names are the one part of a zip nothing else in the bundle redacts."""
        names = " ".join(bundle.members)

        assert _ENV_SECRET not in names
        assert _WORKFLOW_SECRET not in names

    def test_what_was_removed_is_still_visible_as_removed(self, bundle: _Bundle) -> None:
        """A file scrubbed to nothing reads as a file that never had anything in it."""
        session_log = bundle.members["logs/session.log"]

        assert REDACTED in session_log
        # The surrounding text survives: the point of a log is what was happening around it.
        assert "connecting with" in session_log

    def test_the_manifest_counts_what_was_removed(self, bundle: _Bundle) -> None:
        """'0 hidden' against '5 hidden' is how absent is told apart from redacted.

        Read as JSON rather than searched as text: `"total": 0` is one particular spelling,
        so an indent width away from the manifest's own, the search finds nothing and the
        test passes for a bundle that counted nothing.
        """
        redaction = json.loads(bundle.members["manifest.json"])["redaction"]

        assert redaction["total"] > 0
        assert redaction["counts"][RedactionReason.KNOWN_SECRET_VALUE] >= 1

    def test_a_credential_named_setting_keeps_its_key_and_loses_its_value(self, bundle: _Bundle) -> None:
        """Which settings are filled in is the diagnostic signal; the values never are."""
        report = bundle.members["report.json"]

        assert "api_key" in report
        assert REDACTED in report

    def test_a_secret_pasted_into_an_innocently_named_setting_is_removed_too(self, bundle: _Bundle) -> None:
        """`last_used_endpoint` matches no credential-name rule, so only the value can catch it."""
        report = bundle.members["report.json"]

        assert "last_used_endpoint" in report
        assert _ENV_SECRET not in report
