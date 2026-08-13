"""Unit tests for ffmpeg_cache module."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import static_ffmpeg.run
from xdg_base_dirs import xdg_data_home

from griptape_nodes.utils.ffmpeg_cache import redirect_ffmpeg_cache, resolve_ffmpeg_directory


@pytest.fixture(autouse=True)
def _restore_static_ffmpeg_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore `static_ffmpeg`'s module globals after each test.

    `redirect_ffmpeg_cache` mutates process-wide state. Leaking it would change where
    unrelated tests in the same session look for ffmpeg.
    """
    monkeypatch.setattr(static_ffmpeg.run, "SELF_DIR", static_ffmpeg.run.SELF_DIR)
    monkeypatch.setattr(static_ffmpeg.run, "LOCK_FILE", static_ffmpeg.run.LOCK_FILE)


class TestResolveFfmpegDirectory:
    def test_empty_uses_xdg_data_home(self) -> None:
        assert resolve_ffmpeg_directory("") == xdg_data_home() / "griptape_nodes" / "ffmpeg"

    def test_configured_path_used_as_is(self, tmp_path: Path) -> None:
        assert resolve_ffmpeg_directory(str(tmp_path)) == tmp_path

    def test_tilde_expanded(self) -> None:
        assert resolve_ffmpeg_directory("~/ffmpeg") == Path.home() / "ffmpeg"

    def test_never_relative_to_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A relative value stays relative -- it is not resolved against the workspace.

        Unlike the sibling File System settings, this one is machine-scoped on purpose.
        """
        monkeypatch.chdir(tmp_path)
        assert resolve_ffmpeg_directory("ffmpeg-cache") == Path("ffmpeg-cache")


class TestRedirectFfmpegCache:
    def test_redirects_lock_file(self, tmp_path: Path) -> None:
        redirect_ffmpeg_cache(tmp_path)

        assert str(tmp_path / "lock.file") == static_ffmpeg.run.LOCK_FILE

    def test_redirects_download_target(self, tmp_path: Path) -> None:
        """The regression assertion: the binaries move, not just the lock.

        `get_platform_dir()` derives its path from `SELF_DIR` at call time, so redirecting
        `SELF_DIR` is what keeps the download and extract out of the read-only package dir.
        """
        redirect_ffmpeg_cache(tmp_path)

        assert Path(static_ffmpeg.run.get_platform_dir()).is_relative_to(tmp_path)

    def test_creates_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "ffmpeg"

        redirect_ffmpeg_cache(target)

        assert target.is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        redirect_ffmpeg_cache(tmp_path)
        first = (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)

        redirect_ffmpeg_cache(tmp_path)

        assert first == (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)

    def test_last_call_wins(self, tmp_path: Path) -> None:
        redirect_ffmpeg_cache(tmp_path / "first")
        redirect_ffmpeg_cache(tmp_path / "second")

        assert str(tmp_path / "second") == static_ffmpeg.run.SELF_DIR

    def test_unwritable_directory_leaves_globals_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A read-only target must not be redirected into, or we trade one write error for another."""
        original = (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)

        def _read_only_mkdir(*_args: object, **_kwargs: object) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "mkdir", _read_only_mkdir)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            redirect_ffmpeg_cache(tmp_path / "read-only")

        assert original == (static_ffmpeg.run.SELF_DIR, static_ffmpeg.run.LOCK_FILE)
        assert "ffmpeg_directory" in caplog.text

    def test_unwritable_directory_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _read_only_mkdir(*_args: object, **_kwargs: object) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "mkdir", _read_only_mkdir)

        redirect_ffmpeg_cache(tmp_path / "read-only")
