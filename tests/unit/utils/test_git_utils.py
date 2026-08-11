"""Unit tests for git_utils module."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.utils.git_utils import (
    _GIT_ALLOWED_PROTOCOLS,
    GitCloneError,
    GitError,
    GitNotFoundError,
    GitPullError,
    GitRefError,
    GitRemoteError,
    GitRepositoryError,
    _git_env,
    clone_repository,
    extract_repo_name_from_url,
    get_current_ref,
    get_current_tag,
    get_git_info,
    get_git_remote,
    get_git_repository_root,
    get_local_commit_sha,
    git_update_from_remote,
    has_uncommitted_changes,
    is_git_repository,
    is_git_url,
    is_on_tag,
    normalize_github_url,
    parse_commit_datetime,
    parse_git_url_with_ref,
    remote_ref_exists,
    sparse_checkout_library_json,
    switch_branch,
    switch_branch_or_tag,
    update_library_git,
    update_to_moving_tag,
)

if TYPE_CHECKING:
    from collections.abc import Generator


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command with a throwaway identity so commits succeed without machine git config."""
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def make_origin_repo(origin_path: Path, *, library_version: str = "1.2.3") -> Path:
    """Create a git repository at origin_path with a committed griptape_nodes_library.json.

    Pins the initial branch to "main" instead of relying on the local machine's
    init.defaultBranch configuration.
    """
    origin_path.mkdir(parents=True, exist_ok=True)
    run_git(origin_path, "init", "-b", "main")
    library_json = origin_path / "griptape_nodes_library.json"
    library_json.write_text(json.dumps({"metadata": {"library_version": library_version}}), encoding="utf-8")
    run_git(origin_path, "add", ".")
    run_git(origin_path, "commit", "-m", "initial commit")
    return origin_path


def clone_repo(origin_path: Path, clone_path: Path) -> Path:
    """Clone origin_path into clone_path with the git CLI directly, bypassing the code under test."""
    run_git(clone_path.parent, "clone", str(origin_path), str(clone_path))
    return clone_path


def head_sha(repo_path: Path) -> str:
    """Return the full commit SHA that HEAD points to in repo_path."""
    return run_git(repo_path, "rev-parse", "HEAD").stdout.strip()


class TestParseGitUrlWithRef:
    """Test parse_git_url_with_ref function."""

    def test_parse_git_url_with_ref_returns_url_and_ref_for_https_url(self) -> None:
        """Test that HTTPS URL with @ref is parsed correctly."""
        url, ref = parse_git_url_with_ref("https://github.com/user/repo@stable")

        assert url == "https://github.com/user/repo"
        assert ref == "stable"

    def test_parse_git_url_with_ref_returns_url_and_ref_for_shorthand(self) -> None:
        """Test that GitHub shorthand with @ref is parsed correctly."""
        url, ref = parse_git_url_with_ref("user/repo@main")

        assert url == "user/repo"
        assert ref == "main"

    def test_parse_git_url_with_ref_returns_url_and_none_for_url_without_ref(self) -> None:
        """Test that URL without @ref returns None for ref."""
        url, ref = parse_git_url_with_ref("https://github.com/user/repo")

        assert url == "https://github.com/user/repo"
        assert ref is None

    def test_parse_git_url_with_ref_returns_url_and_none_for_shorthand_without_ref(self) -> None:
        """Test that shorthand without @ref returns None for ref."""
        url, ref = parse_git_url_with_ref("user/repo")

        assert url == "user/repo"
        assert ref is None

    def test_parse_git_url_with_ref_handles_ssh_url_with_ref(self) -> None:
        """Test that SSH URL with @ref is parsed correctly."""
        url, ref = parse_git_url_with_ref("git@github.com:user/repo@stable")

        assert url == "git@github.com:user/repo"
        assert ref == "stable"

    def test_parse_git_url_with_ref_handles_ssh_url_without_ref(self) -> None:
        """Test that SSH URL without @ref returns None for ref."""
        url, ref = parse_git_url_with_ref("git@github.com:user/repo.git")

        assert url == "git@github.com:user/repo.git"
        assert ref is None

    def test_parse_git_url_with_ref_handles_url_with_git_suffix_and_ref(self) -> None:
        """Test that URL with .git suffix and @ref is parsed correctly."""
        url, ref = parse_git_url_with_ref("https://github.com/user/repo.git@v1.0.0")

        assert url == "https://github.com/user/repo.git"
        assert ref == "v1.0.0"

    def test_parse_git_url_with_ref_strips_whitespace(self) -> None:
        """Test that whitespace is stripped before parsing."""
        url, ref = parse_git_url_with_ref("  user/repo@stable  ")

        assert url == "user/repo"
        assert ref == "stable"


class TestExtractRepoNameFromUrl:
    """Test extract_repo_name_from_url function."""

    def test_extract_repo_name_from_https_url(self) -> None:
        """Test that repo name is extracted from HTTPS URL."""
        result = extract_repo_name_from_url("https://github.com/user/my-repo")

        assert result == "my-repo"

    def test_extract_repo_name_from_https_url_with_git_suffix(self) -> None:
        """Test that repo name is extracted from HTTPS URL with .git suffix."""
        result = extract_repo_name_from_url("https://github.com/user/my-repo.git")

        assert result == "my-repo"

    def test_extract_repo_name_from_https_url_with_ref(self) -> None:
        """Test that repo name is extracted from HTTPS URL with @ref."""
        result = extract_repo_name_from_url("https://github.com/user/my-repo@stable")

        assert result == "my-repo"

    def test_extract_repo_name_from_https_url_with_git_suffix_and_ref(self) -> None:
        """Test that repo name is extracted from HTTPS URL with .git and @ref."""
        result = extract_repo_name_from_url("https://github.com/user/my-repo.git@stable")

        assert result == "my-repo"

    def test_extract_repo_name_from_shorthand(self) -> None:
        """Test that repo name is extracted from GitHub shorthand."""
        result = extract_repo_name_from_url("user/my-repo")

        assert result == "my-repo"

    def test_extract_repo_name_from_shorthand_with_ref(self) -> None:
        """Test that repo name is extracted from GitHub shorthand with @ref."""
        result = extract_repo_name_from_url("user/my-repo@main")

        assert result == "my-repo"

    def test_extract_repo_name_from_ssh_url(self) -> None:
        """Test that repo name is extracted from SSH URL."""
        result = extract_repo_name_from_url("git@github.com:user/my-repo.git")

        assert result == "my-repo"

    def test_extract_repo_name_from_ssh_url_with_ref(self) -> None:
        """Test that repo name is extracted from SSH URL with @ref."""
        result = extract_repo_name_from_url("git@github.com:user/my-repo@stable")

        assert result == "my-repo"

    def test_extract_repo_name_from_url_with_trailing_slash_before_ref(self) -> None:
        """A trailing slash right before the @ref must not swallow the repo name."""
        result = extract_repo_name_from_url("https://github.com/user/my-repo/@stable")

        assert result == "my-repo"


class TestIsGitUrl:
    """Test is_git_url function."""

    def test_is_git_url_returns_true_for_https_url(self) -> None:
        """Test that HTTPS URLs are recognized as git URLs."""
        result = is_git_url("https://github.com/user/repo.git")

        assert result is True

    def test_is_git_url_returns_true_for_http_url(self) -> None:
        """Test that HTTP URLs are recognized as git URLs."""
        result = is_git_url("http://github.com/user/repo.git")

        assert result is True

    def test_is_git_url_returns_true_for_git_protocol_url(self) -> None:
        """Test that git:// URLs are recognized as git URLs."""
        result = is_git_url("git://github.com/user/repo.git")

        assert result is True

    def test_is_git_url_returns_true_for_ssh_protocol_url(self) -> None:
        """Test that ssh:// URLs are recognized as git URLs."""
        result = is_git_url("ssh://git@github.com/user/repo.git")

        assert result is True

    def test_is_git_url_returns_true_for_git_at_ssh_url(self) -> None:
        """Test that git@... URLs are recognized as git URLs."""
        result = is_git_url("git@github.com:user/repo.git")

        assert result is True

    def test_is_git_url_returns_false_for_plain_text(self) -> None:
        """Test that plain text is not recognized as a git URL."""
        result = is_git_url("user/repo")

        assert result is False

    def test_is_git_url_returns_false_for_local_path(self) -> None:
        """Test that local paths are not recognized as a git URL."""
        result = is_git_url("/home/user/repo")

        assert result is False


class TestNormalizeGithubUrl:
    """Test normalize_github_url function."""

    def test_normalize_github_shorthand_to_https_url(self) -> None:
        """Test that GitHub shorthand is converted to HTTPS URL."""
        result = normalize_github_url("user/repo")

        assert result == "https://github.com/user/repo.git"

    def test_normalize_github_shorthand_with_organization(self) -> None:
        """Test that organization shorthand is converted correctly."""
        result = normalize_github_url("griptape-ai/griptape-nodes")

        assert result == "https://github.com/griptape-ai/griptape-nodes.git"

    def test_normalize_adds_git_suffix_to_github_https_url(self) -> None:
        """Test that .git suffix is added to GitHub HTTPS URLs."""
        result = normalize_github_url("https://github.com/user/repo")

        assert result == "https://github.com/user/repo.git"

    def test_normalize_preserves_git_suffix_on_github_url(self) -> None:
        """Test that existing .git suffix is preserved."""
        result = normalize_github_url("https://github.com/user/repo.git")

        assert result == "https://github.com/user/repo.git"

    def test_normalize_preserves_ssh_github_url(self) -> None:
        """Test that SSH GitHub URLs are preserved."""
        result = normalize_github_url("git@github.com:user/repo.git")

        assert result == "git@github.com:user/repo.git"

    def test_normalize_preserves_non_github_urls(self) -> None:
        """Test that non-GitHub URLs are passed through unchanged."""
        gitlab_url = "https://gitlab.com/user/repo"
        result = normalize_github_url(gitlab_url)

        assert result == gitlab_url

    def test_normalize_strips_trailing_slash(self) -> None:
        """Test that trailing slashes are removed."""
        result = normalize_github_url("user/repo/")

        assert result == "https://github.com/user/repo.git"

    def test_normalize_strips_leading_and_trailing_whitespace(self) -> None:
        """Test that whitespace is stripped."""
        result = normalize_github_url("  user/repo  ")

        assert result == "https://github.com/user/repo.git"

    def test_normalize_github_shorthand_with_ref(self) -> None:
        """Test that GitHub shorthand with @ref is converted correctly."""
        result = normalize_github_url("user/repo@stable")

        assert result == "https://github.com/user/repo.git@stable"

    def test_normalize_github_https_url_with_ref(self) -> None:
        """Test that HTTPS URL with @ref gets .git suffix before @ref."""
        result = normalize_github_url("https://github.com/user/repo@main")

        assert result == "https://github.com/user/repo.git@main"

    def test_normalize_github_https_url_with_git_suffix_and_ref(self) -> None:
        """Test that HTTPS URL with .git and @ref preserves both."""
        result = normalize_github_url("https://github.com/user/repo.git@v1.0.0")

        assert result == "https://github.com/user/repo.git@v1.0.0"

    def test_normalize_preserves_ssh_github_url_with_ref(self) -> None:
        """Test that SSH GitHub URLs with @ref are preserved."""
        result = normalize_github_url("git@github.com:user/repo.git@stable")

        assert result == "git@github.com:user/repo.git@stable"


class TestIsGitRepository:
    """Test is_git_repository function."""

    def test_is_git_repository_returns_false_when_path_does_not_exist(self) -> None:
        """Test that False is returned when path doesn't exist."""
        non_existent = Path("/non/existent/path")

        result = is_git_repository(non_existent)

        assert result is False

    def test_is_git_repository_returns_false_when_path_is_not_directory(self) -> None:
        """Test that False is returned when path is a file."""
        with tempfile.NamedTemporaryFile() as tmp:
            result = is_git_repository(Path(tmp.name))

            assert result is False

    def test_is_git_repository_returns_false_when_not_git_repo(self) -> None:
        """Test that False is returned when directory is not a git repository."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = is_git_repository(Path(tmpdir))

            assert result is False

    def test_is_git_repository_returns_true_when_git_repo(self) -> None:
        """Test that True is returned for valid git repository."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .git directory to simulate a git repository
            git_dir = Path(tmpdir) / ".git"
            git_dir.mkdir()

            result = is_git_repository(Path(tmpdir))

            assert result is True

    def test_is_git_repository_returns_true_when_parent_is_git_repo(self) -> None:
        """Test that True is returned when parent directory is a git repository."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .git directory in the parent to simulate a git repository
            git_dir = Path(tmpdir) / ".git"
            git_dir.mkdir()

            # Create a subdirectory (like a library folder in a monorepo)
            subdir = Path(tmpdir) / "library-name"
            subdir.mkdir()

            result = is_git_repository(subdir)

            assert result is True


class TestGetGitRemote:
    """Test get_git_remote function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_git_remote_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        result = get_git_remote(temp_dir)

        assert result is None

    def test_get_git_remote_returns_none_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that None is returned when no origin remote exists."""
        repo = make_origin_repo(temp_dir / "repo")

        result = get_git_remote(repo)

        assert result is None

    def test_get_git_remote_returns_url_when_origin_exists(self, temp_dir: Path) -> None:
        """Test that remote URL is returned when origin exists."""
        repo = make_origin_repo(temp_dir / "repo")
        expected_url = "https://github.com/user/repo.git"
        run_git(repo, "remote", "add", "origin", expected_url)

        result = get_git_remote(repo)

        assert result == expected_url


class TestGetCurrentRef:
    """Test get_current_ref function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_current_ref_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        result = get_current_ref(temp_dir)

        assert result is None

    def test_get_current_ref_returns_branch_name_when_on_branch(self, temp_dir: Path) -> None:
        """Test that branch name is returned when on a branch."""
        repo = make_origin_repo(temp_dir / "repo")

        result = get_current_ref(repo)

        assert result == "main"

    def test_get_current_ref_returns_tag_name_when_head_detached_and_tagged(self, temp_dir: Path) -> None:
        """Test that tag name is returned when HEAD is detached on a tagged commit."""
        repo = make_origin_repo(temp_dir / "repo")
        run_git(repo, "tag", "v1.0.0")
        run_git(repo, "checkout", "--detach", "v1.0.0")

        result = get_current_ref(repo)

        assert result == "v1.0.0"

    def test_get_current_ref_returns_commit_sha_when_head_detached_and_not_tagged(self, temp_dir: Path) -> None:
        """Test that commit SHA is returned when HEAD is detached and not tagged."""
        repo = make_origin_repo(temp_dir / "repo")
        sha = head_sha(repo)
        run_git(repo, "checkout", "--detach", sha)

        result = get_current_ref(repo)

        assert result == sha

    def test_get_current_ref_returns_none_when_head_is_unborn(self, temp_dir: Path) -> None:
        """Test that None is returned for a freshly initialized repository with no commits."""
        repo = temp_dir / "unborn"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")

        result = get_current_ref(repo)

        assert result is None


class TestGetCurrentTag:
    """Test get_current_tag function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_current_tag_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        result = get_current_tag(temp_dir)

        assert result is None

    def test_get_current_tag_returns_none_when_head_is_unborn(self, temp_dir: Path) -> None:
        """Test that None is returned for a freshly initialized repository with no commits."""
        repo = temp_dir / "unborn"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")

        result = get_current_tag(repo)

        assert result is None

    def test_get_current_tag_returns_none_when_head_not_tagged(self, temp_dir: Path) -> None:
        """Test that None is returned when HEAD has no tag pointing at it."""
        repo = make_origin_repo(temp_dir / "repo")

        result = get_current_tag(repo)

        assert result is None

    def test_get_current_tag_returns_tag_name_when_head_is_tagged(self, temp_dir: Path) -> None:
        """Test that the tag name is returned when a tag points at HEAD, even on a branch."""
        repo = make_origin_repo(temp_dir / "repo")
        run_git(repo, "tag", "v1.0.0")

        result = get_current_tag(repo)

        assert result == "v1.0.0"


class TestIsOnTag:
    """Test is_on_tag function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_is_on_tag_returns_false_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that False is returned when path is not a git repository."""
        result = is_on_tag(temp_dir)

        assert result is False

    def test_is_on_tag_returns_false_when_head_not_tagged(self, temp_dir: Path) -> None:
        """Test that False is returned when HEAD has no tag pointing at it."""
        repo = make_origin_repo(temp_dir / "repo")

        result = is_on_tag(repo)

        assert result is False

    def test_is_on_tag_returns_true_when_head_is_tagged(self, temp_dir: Path) -> None:
        """Test that True is returned when a tag points at HEAD."""
        repo = make_origin_repo(temp_dir / "repo")
        run_git(repo, "tag", "v1.0.0")

        result = is_on_tag(repo)

        assert result is True


class TestGetLocalCommitSha:
    """Test get_local_commit_sha function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_local_commit_sha_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        result = get_local_commit_sha(temp_dir)

        assert result is None

    def test_get_local_commit_sha_returns_none_when_head_is_unborn(self, temp_dir: Path) -> None:
        """Test that None is returned for a freshly initialized repository with no commits."""
        repo = temp_dir / "unborn"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")

        result = get_local_commit_sha(repo)

        assert result is None

    def test_get_local_commit_sha_returns_full_commit_sha_when_head_has_commits(self, temp_dir: Path) -> None:
        """Test that the full HEAD commit SHA is returned."""
        repo = make_origin_repo(temp_dir / "repo")

        result = get_local_commit_sha(repo)

        assert result == head_sha(repo)
        assert result is not None
        assert len(result) == 40  # noqa: PLR2004


class TestGetGitRepositoryRoot:
    """Test get_git_repository_root function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_git_repository_root_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        result = get_git_repository_root(temp_dir)

        assert result is None

    def test_get_git_repository_root_returns_root_when_called_on_root(self, temp_dir: Path) -> None:
        """Test that the repository root is returned when called on the root itself."""
        repo = make_origin_repo(temp_dir / "repo")

        result = get_git_repository_root(repo)

        assert result == repo

    def test_get_git_repository_root_returns_root_when_called_on_subdirectory(self, temp_dir: Path) -> None:
        """Test that the repository root is returned when called on a subdirectory."""
        repo = make_origin_repo(temp_dir / "repo")
        subdir = repo / "subdir"
        subdir.mkdir()

        result = get_git_repository_root(subdir)

        assert result == repo


class TestCloneRepositoryWorkingDirectory:
    """Test clone_repository's independence from the working directory."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_clone_repository_ignores_a_broken_repository_in_the_working_directory(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a broken repository at the working directory doesn't fail the clone."""
        origin = make_origin_repo(temp_dir / "origin")
        broken = temp_dir / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/worktrees/gone\n")
        monkeypatch.chdir(broken)

        clone_repository(str(origin), temp_dir / "clone")

        assert (temp_dir / "clone" / "griptape_nodes_library.json").exists()

    def test_clone_repository_clones_relative_target_into_the_working_directory(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a relative target lands in the caller's cwd, not the internal scratch dir."""
        origin = make_origin_repo(temp_dir / "origin")
        workdir = temp_dir / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        clone_repository(str(origin), Path("nested/clone"))

        assert (workdir / "nested" / "clone" / "griptape_nodes_library.json").exists()

    def test_clone_repository_rejects_remote_helper_url(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a '<helper>::<url>' URL cannot make git exec a transport binary from PATH.

        git resolves an unrecognized URL prefix to a git-remote-<prefix> executable and runs it.
        A planted helper stands in for that binary: it must never be executed, so the URL has to
        be refused on the transport policy rather than on the helper's own exit code.
        """
        marker = temp_dir / "helper-ran"
        bin_dir = temp_dir / "bin"
        bin_dir.mkdir()
        helper = bin_dir / "git-remote-weirdhelper"
        helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        helper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        with pytest.raises(GitCloneError):
            clone_repository("weirdhelper::whatever", temp_dir / "clone")

        assert not marker.exists()

    def test_clone_repository_rejects_ext_transport_url(self, temp_dir: Path) -> None:
        """Test that an 'ext::<command>' URL cannot make git run an arbitrary shell command.

        The ext transport hands its argument to a shell, so a URL reaching it is remote code
        execution. The marker file proves the command never ran, rather than only that the
        clone reported failure.
        """
        marker = temp_dir / "ext-ran"

        with pytest.raises(GitCloneError):
            clone_repository(f'ext::sh -c "touch {marker}"', temp_dir / "clone")

        assert not marker.exists()


class TestGitEnvironment:
    """Test the environment git subprocesses are given."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_git_env_pins_the_transport_allowlist_and_disables_prompting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the hardening defaults are present when nothing is inherited."""
        monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
        monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)

        env = _git_env()

        assert env["GIT_ALLOW_PROTOCOL"] == _GIT_ALLOWED_PROTOCOLS
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_git_env_lets_an_inherited_value_override_a_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a launcher can admit a transport the allowlist omits."""
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

        env = _git_env()

        assert env["GIT_ALLOW_PROTOCOL"] == "https"
        assert env["GIT_TERMINAL_PROMPT"] == "1"

    def test_git_env_reaches_the_subprocess(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the built environment is what git actually runs with.

        Without this, the allowlist could be computed correctly and never passed along.
        """
        monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
        origin = make_origin_repo(temp_dir / "origin")

        with patch("griptape_nodes.utils.git_utils.subprocess.run", wraps=subprocess.run) as mock_run:
            clone_repository(str(origin), temp_dir / "clone")

        assert mock_run.call_args_list
        for call in mock_run.call_args_list:
            assert call.kwargs["env"]["GIT_ALLOW_PROTOCOL"] == _GIT_ALLOWED_PROTOCOLS
            assert call.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


class TestGetGitInfo:
    """Test get_git_info function.

    get_git_info degrades to (None, None) where get_git_remote() and get_current_ref() would
    each raise or independently re-check is_git_repository().
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_git_info_returns_none_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that (None, None) is returned when path is not a git repository."""
        git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote is None
        assert git_ref is None

    def test_get_git_info_returns_remote_and_branch_when_on_branch(self, temp_dir: Path) -> None:
        """Test that both remote URL and branch name are returned when on a branch."""
        repo = make_origin_repo(temp_dir / "repo")
        expected_url = "https://github.com/user/repo.git"
        run_git(repo, "remote", "add", "origin", expected_url)

        git_remote, git_ref = get_git_info(repo)

        assert git_remote == expected_url
        assert git_ref == "main"

    def test_get_git_info_returns_none_remote_when_no_origin(self, temp_dir: Path) -> None:
        """Test that remote is None when no origin is configured."""
        repo = make_origin_repo(temp_dir / "repo")

        git_remote, git_ref = get_git_info(repo)

        assert git_remote is None
        assert git_ref == "main"

    def test_get_git_info_returns_commit_sha_when_head_detached_and_no_tag(self, temp_dir: Path) -> None:
        """Test that the commit SHA is returned as the ref for a detached, untagged HEAD."""
        repo = make_origin_repo(temp_dir / "repo")
        sha = head_sha(repo)
        run_git(repo, "checkout", "--detach", sha)

        _git_remote, git_ref = get_git_info(repo)

        assert git_ref == sha

    def test_get_git_info_returns_tag_name_when_head_on_tag(self, temp_dir: Path) -> None:
        """Test that the tag name is returned as the ref for a detached, tagged HEAD."""
        repo = make_origin_repo(temp_dir / "repo")
        run_git(repo, "tag", "v1.0.0")
        run_git(repo, "checkout", "--detach", "v1.0.0")

        _git_remote, git_ref = get_git_info(repo)

        assert git_ref == "v1.0.0"

    def test_get_git_info_returns_none_ref_when_head_unborn(self, temp_dir: Path) -> None:
        """Test that ref is None for a freshly initialized repository with no commits."""
        repo = temp_dir / "unborn"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")

        git_remote, git_ref = get_git_info(repo)

        assert git_remote is None
        assert git_ref is None


class TestHasUncommittedChanges:
    """Test has_uncommitted_changes function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_has_uncommitted_changes_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with pytest.raises(GitRepositoryError, match="is not a git repository"):
            has_uncommitted_changes(temp_dir)

    def test_has_uncommitted_changes_returns_false_when_clean(self, temp_dir: Path) -> None:
        """Test that False is returned when the working tree is clean."""
        repo = make_origin_repo(temp_dir / "repo")

        result = has_uncommitted_changes(repo)

        assert result is False

    def test_has_uncommitted_changes_returns_true_when_tracked_file_modified(self, temp_dir: Path) -> None:
        """Test that True is returned when a tracked file has local modifications."""
        repo = make_origin_repo(temp_dir / "repo")
        (repo / "griptape_nodes_library.json").write_text("changed", encoding="utf-8")

        result = has_uncommitted_changes(repo)

        assert result is True

    def test_has_uncommitted_changes_returns_true_when_untracked_file_present(self, temp_dir: Path) -> None:
        """Test that True is returned when an untracked file is present."""
        repo = make_origin_repo(temp_dir / "repo")
        (repo / "untracked.txt").write_text("new", encoding="utf-8")

        result = has_uncommitted_changes(repo)

        assert result is True


class TestGitUpdateFromRemote:
    """Test git_update_from_remote function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_git_update_from_remote_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with pytest.raises(GitRepositoryError, match="is not a git repository"):
            git_update_from_remote(temp_dir)

    def test_git_update_from_remote_raises_error_when_head_detached(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when HEAD is detached."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "checkout", "--detach", head_sha(clone))

        with pytest.raises(GitPullError, match="detached HEAD"):
            git_update_from_remote(clone)

    def test_git_update_from_remote_raises_error_when_no_upstream_branch(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when no upstream branch is set."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "checkout", "-b", "untracked")

        with pytest.raises(GitPullError, match="No upstream branch"):
            git_update_from_remote(clone)

    def test_git_update_from_remote_raises_error_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when no origin remote exists."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        # Renaming keeps the branch's upstream tracking resolvable while origin disappears,
        # isolating the "no origin remote" check from the "no upstream" check.
        run_git(clone, "remote", "rename", "origin", "other")

        with pytest.raises(GitPullError, match="No origin remote"):
            git_update_from_remote(clone)

    def test_git_update_from_remote_raises_error_when_uncommitted_changes_and_not_overwriting(
        self, temp_dir: Path
    ) -> None:
        """Test that GitPullError is raised when uncommitted changes exist and overwrite_existing is False."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        (clone / "griptape_nodes_library.json").write_text("dirty", encoding="utf-8")

        with pytest.raises(GitPullError, match="uncommitted changes"):
            git_update_from_remote(clone)

    def test_git_update_from_remote_discards_uncommitted_changes_when_overwrite_existing_true(
        self, temp_dir: Path
    ) -> None:
        """Test that uncommitted changes are discarded when overwrite_existing is True."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        (clone / "griptape_nodes_library.json").write_text("dirty", encoding="utf-8")

        git_update_from_remote(clone, overwrite_existing=True)

        assert has_uncommitted_changes(clone) is False

    def test_git_update_from_remote_resets_to_upstream_tip_after_origin_moves_forward(self, temp_dir: Path) -> None:
        """Test that the local branch is reset to match the remote after origin advances."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        (origin / "extra.txt").write_text("extra", encoding="utf-8")
        run_git(origin, "add", ".")
        run_git(origin, "commit", "-m", "advance origin")

        git_update_from_remote(clone)

        assert get_local_commit_sha(clone) == head_sha(origin)
        assert (clone / "extra.txt").exists()


class TestUpdateToMovingTag:
    """Test update_to_moving_tag function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_update_to_moving_tag_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with pytest.raises(GitRepositoryError, match="is not a git repository"):
            update_to_moving_tag(temp_dir, "latest")

    def test_update_to_moving_tag_raises_error_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when no origin remote exists."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "latest")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "remote", "rename", "origin", "other")

        with pytest.raises(GitPullError, match="No origin remote"):
            update_to_moving_tag(clone, "latest")

    def test_update_to_moving_tag_raises_error_when_tag_not_found_on_remote(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when the tag does not exist on the remote."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with pytest.raises(GitPullError, match="not found"):
            update_to_moving_tag(clone, "latest")

    def test_update_to_moving_tag_follows_tag_when_origin_force_moves_it(self, temp_dir: Path) -> None:
        """Test that the tag is followed to a new commit after origin force-moves it."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "latest")
        clone = clone_repo(origin, temp_dir / "clone")

        update_to_moving_tag(clone, "latest")

        assert get_local_commit_sha(clone) == head_sha(origin)

        (origin / "extra.txt").write_text("extra", encoding="utf-8")
        run_git(origin, "add", ".")
        run_git(origin, "commit", "-m", "advance origin")
        run_git(origin, "tag", "-f", "latest")
        moved_sha = head_sha(origin)

        update_to_moving_tag(clone, "latest")

        assert get_local_commit_sha(clone) == moved_sha

    def test_update_to_moving_tag_raises_error_when_uncommitted_changes_and_not_overwriting(
        self, temp_dir: Path
    ) -> None:
        """Test that GitPullError is raised when uncommitted changes exist and overwrite_existing is False."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "latest")
        clone = clone_repo(origin, temp_dir / "clone")
        (clone / "griptape_nodes_library.json").write_text("dirty", encoding="utf-8")

        with pytest.raises(GitPullError, match="uncommitted changes"):
            update_to_moving_tag(clone, "latest")

    def test_update_to_moving_tag_discards_uncommitted_changes_when_overwrite_existing_true(
        self, temp_dir: Path
    ) -> None:
        """Test that uncommitted changes are discarded when overwrite_existing is True."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "latest")
        clone = clone_repo(origin, temp_dir / "clone")
        (clone / "griptape_nodes_library.json").write_text("dirty", encoding="utf-8")

        update_to_moving_tag(clone, "latest", overwrite_existing=True)

        assert has_uncommitted_changes(clone) is False


class TestUpdateLibraryGit:
    """Test update_library_git function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_update_library_git_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with pytest.raises(GitRepositoryError, match="is not a git repository"):
            update_library_git(temp_dir)

    def test_update_library_git_dispatches_to_branch_update_when_on_branch(self, temp_dir: Path) -> None:
        """Test that update_library_git resets to the remote tip for a branch-based checkout."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        (origin / "extra.txt").write_text("extra", encoding="utf-8")
        run_git(origin, "add", ".")
        run_git(origin, "commit", "-m", "advance origin")

        update_library_git(clone)

        assert get_local_commit_sha(clone) == head_sha(origin)

    def test_update_library_git_dispatches_to_tag_update_when_detached_head_on_tag(self, temp_dir: Path) -> None:
        """Test that update_library_git follows a moving tag for a tag-based checkout."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "stable")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "checkout", "--detach", "stable")

        (origin / "extra.txt").write_text("extra", encoding="utf-8")
        run_git(origin, "add", ".")
        run_git(origin, "commit", "-m", "advance origin")
        run_git(origin, "tag", "-f", "stable")
        moved_sha = head_sha(origin)

        update_library_git(clone)

        assert get_local_commit_sha(clone) == moved_sha

    def test_update_library_git_raises_error_when_detached_head_without_known_tag(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised for a detached HEAD that isn't on a known tag."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "checkout", "--detach", head_sha(clone))

        with pytest.raises(GitPullError, match="not on a known tag"):
            update_library_git(clone)


class TestSwitchBranch:
    """Test switch_branch function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_switch_branch_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with pytest.raises(GitRepositoryError, match="is not a git repository"):
            switch_branch(temp_dir, "main")

    def test_switch_branch_raises_error_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised when no origin remote exists."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "remote", "rename", "origin", "other")

        with pytest.raises(GitRefError, match="No origin remote"):
            switch_branch(clone, "main")

    def test_switch_branch_checks_out_existing_local_branch(self, temp_dir: Path) -> None:
        """Test that an existing local branch is checked out."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "branch", "feature")

        switch_branch(clone, "feature")

        assert get_current_ref(clone) == "feature"

    def test_switch_branch_creates_tracking_branch_from_remote_when_only_remote_has_it(self, temp_dir: Path) -> None:
        """Test that a tracking branch is created from the remote when only origin has it."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(origin, "branch", "feature")

        switch_branch(clone, "feature")

        assert get_current_ref(clone) == "feature"

    def test_switch_branch_raises_error_when_branch_not_found(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised when the branch doesn't exist locally or remotely."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with pytest.raises(GitRefError, match="not found"):
            switch_branch(clone, "nonexistent")


class TestSwitchBranchOrTag:
    """Test switch_branch_or_tag function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_switch_branch_or_tag_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with pytest.raises(GitRepositoryError, match="is not a git repository"):
            switch_branch_or_tag(temp_dir, "main")

    def test_switch_branch_or_tag_checks_out_tag(self, temp_dir: Path) -> None:
        """Test that a tag is checked out as a detached HEAD."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "v1.0.0")
        clone = clone_repo(origin, temp_dir / "clone")

        switch_branch_or_tag(clone, "v1.0.0")

        assert get_current_tag(clone) == "v1.0.0"

    def test_switch_branch_or_tag_checks_out_remote_only_branch(self, temp_dir: Path) -> None:
        """Test that a branch only present on the remote is checked out as a tracking branch."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(origin, "branch", "feature")

        switch_branch_or_tag(clone, "feature")

        assert get_current_ref(clone) == "feature"

    def test_switch_branch_or_tag_checks_out_local_branch(self, temp_dir: Path) -> None:
        """Test that a local-only branch is checked out."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        run_git(clone, "branch", "localonly")

        switch_branch_or_tag(clone, "localonly")

        assert get_current_ref(clone) == "localonly"

    def test_switch_branch_or_tag_raises_error_for_unknown_ref(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised when the ref doesn't exist anywhere."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with pytest.raises(GitRefError, match="not found"):
            switch_branch_or_tag(clone, "nonexistent")


class TestCloneRepository:
    """Test clone_repository function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_clone_repository_raises_error_when_target_exists(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised when target path already exists."""
        existing_path = temp_dir / "existing"
        existing_path.mkdir()

        with pytest.raises(GitCloneError, match="already exists"):
            clone_repository("https://github.com/user/repo.git", existing_path)

    def test_clone_repository_clones_repository(self, temp_dir: Path) -> None:
        """Test that a repository is cloned successfully."""
        origin = make_origin_repo(temp_dir / "origin")
        target = temp_dir / "clone"

        clone_repository(str(origin), target)

        assert (target / ".git").exists()
        assert (target / "griptape_nodes_library.json").exists()
        assert get_current_ref(target) == "main"

    def test_clone_repository_checks_out_specified_branch(self, temp_dir: Path) -> None:
        """Test that the specified branch is checked out after cloning."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "branch", "feature")
        target = temp_dir / "clone"

        clone_repository(str(origin), target, "feature")

        assert get_current_ref(target) == "feature"

    def test_clone_repository_checks_out_specified_tag(self, temp_dir: Path) -> None:
        """Test that the specified tag is checked out as a detached HEAD after cloning."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "v1.0.0")
        target = temp_dir / "clone"

        clone_repository(str(origin), target, "v1.0.0")

        assert get_current_tag(target) == "v1.0.0"

    def test_clone_repository_checks_out_specified_commit(self, temp_dir: Path) -> None:
        """Test that a full commit SHA is checked out as a detached HEAD after cloning."""
        origin = make_origin_repo(temp_dir / "origin")
        sha = head_sha(origin)
        target = temp_dir / "clone"

        clone_repository(str(origin), target, sha)

        assert get_local_commit_sha(target) == sha
        assert get_current_ref(target) == sha

    def test_clone_repository_raises_error_on_bogus_source_url(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised when the source URL doesn't resolve to a repository."""
        target = temp_dir / "clone"

        with pytest.raises(GitCloneError, match="Git error while cloning"):
            clone_repository(str(temp_dir / "does-not-exist"), target)


class TestSparseCheckoutLibraryJson:
    """Test sparse_checkout_library_json function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_sparse_checkout_library_json_returns_metadata_for_default_ref(self, temp_dir: Path) -> None:
        """Test that library version, commit SHA, commit datetime, and library data are returned for HEAD."""
        origin = make_origin_repo(temp_dir / "origin", library_version="1.2.3")

        result = sparse_checkout_library_json(str(origin))

        assert result.library_version == "1.2.3"
        assert len(result.commit_sha) == 40  # noqa: PLR2004
        assert result.commit_sha == head_sha(origin)
        assert result.commit_datetime is not None
        assert result.commit_datetime.tzinfo is not None
        assert result.library_data == {"metadata": {"library_version": "1.2.3"}}

    def test_sparse_checkout_library_json_uses_specified_ref(self, temp_dir: Path) -> None:
        """Test that an older tagged commit is fetched when a ref is specified."""
        origin = make_origin_repo(temp_dir / "origin", library_version="1.0.0")
        run_git(origin, "tag", "v1")
        library_json = origin / "griptape_nodes_library.json"
        library_json.write_text(json.dumps({"metadata": {"library_version": "2.0.0"}}), encoding="utf-8")
        run_git(origin, "add", ".")
        run_git(origin, "commit", "-m", "bump version")

        head_result = sparse_checkout_library_json(str(origin))
        tagged_result = sparse_checkout_library_json(str(origin), ref="v1")

        assert head_result.library_version == "2.0.0"
        assert tagged_result.library_version == "1.0.0"

    def test_sparse_checkout_library_json_raises_error_when_no_library_json_present(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised when the repository has no library JSON file."""
        origin = temp_dir / "origin"
        origin.mkdir()
        run_git(origin, "init", "-b", "main")
        (origin / "README.md").write_text("no library here", encoding="utf-8")
        run_git(origin, "add", ".")
        run_git(origin, "commit", "-m", "initial commit")

        with pytest.raises(GitCloneError, match="No library JSON file found"):
            sparse_checkout_library_json(str(origin))


class TestRemoteRefExists:
    """Test remote_ref_exists function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_remote_ref_exists_returns_true_for_existing_branch(self, temp_dir: Path) -> None:
        """Test that True is returned for a branch that exists on the remote."""
        origin = make_origin_repo(temp_dir / "origin")

        assert remote_ref_exists(str(origin), "main") is True

    def test_remote_ref_exists_returns_true_for_existing_tag(self, temp_dir: Path) -> None:
        """Test that True is returned for a tag that exists on the remote."""
        origin = make_origin_repo(temp_dir / "origin")
        run_git(origin, "tag", "v1.0.0")

        assert remote_ref_exists(str(origin), "v1.0.0") is True

    def test_remote_ref_exists_returns_false_for_unknown_ref(self, temp_dir: Path) -> None:
        """Test that False is returned for a ref that doesn't exist on the remote."""
        origin = make_origin_repo(temp_dir / "origin")

        assert remote_ref_exists(str(origin), "no-such-ref") is False

    def test_remote_ref_exists_returns_false_for_commit_sha(self, temp_dir: Path) -> None:
        """Test that False is returned for a commit SHA, since SHAs aren't advertised as named refs."""
        origin = make_origin_repo(temp_dir / "origin")
        sha = head_sha(origin)

        assert remote_ref_exists(str(origin), sha) is False

    def test_remote_ref_exists_raises_error_when_remote_cannot_be_queried(self, temp_dir: Path) -> None:
        """Test that GitRemoteError is raised when the remote cannot be queried."""
        with pytest.raises(GitRemoteError, match="Failed to query remote refs"):
            remote_ref_exists(str(temp_dir / "not-a-repo"), "main")

    def test_remote_ref_exists_ignores_a_broken_repository_in_the_working_directory(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a broken repository at the working directory doesn't fail the query."""
        origin = make_origin_repo(temp_dir / "origin")
        broken = temp_dir / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/worktrees/gone\n")
        monkeypatch.chdir(broken)

        assert remote_ref_exists(str(origin), "main") is True


class TestOptionLikeArguments:
    """Test that caller-supplied values git would read as options are rejected."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_clone_repository_rejects_option_like_url(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised for a URL that git would parse as an option."""
        with pytest.raises(GitCloneError, match="must not start with"):
            clone_repository("--upload-pack=touch /tmp/pwned", temp_dir / "clone")

    def test_clone_repository_rejects_option_like_ref(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised for a ref that git would parse as an option."""
        origin = make_origin_repo(temp_dir / "origin")

        with pytest.raises(GitCloneError, match="must not start with"):
            clone_repository(str(origin), temp_dir / "clone", "--orphan")

    def test_remote_ref_exists_rejects_option_like_url(self) -> None:
        """Test that GitRemoteError is raised for a URL that git would parse as an option."""
        with pytest.raises(GitRemoteError, match="must not start with"):
            remote_ref_exists("--upload-pack=touch /tmp/pwned", "main")

    def test_sparse_checkout_library_json_rejects_option_like_url(self) -> None:
        """Test that GitCloneError is raised for a URL that git would parse as an option."""
        with pytest.raises(GitCloneError, match="must not start with"):
            sparse_checkout_library_json("--upload-pack=touch /tmp/pwned")

    def test_switch_branch_rejects_option_like_branch(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised for a branch name that git would parse as an option."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with pytest.raises(GitRefError, match="must not start with"):
            switch_branch(clone, "--track")

    def test_switch_branch_or_tag_rejects_option_like_ref(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised for a ref name that git would parse as an option."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with pytest.raises(GitRefError, match="must not start with"):
            switch_branch_or_tag(clone, "--detach")

    def test_update_to_moving_tag_rejects_option_like_tag(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised for a tag name that git would parse as an option."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with pytest.raises(GitPullError, match="must not start with"):
            update_to_moving_tag(clone, "--force")


class TestGitNotInstalled:
    """Test the documented contract for functions when git isn't found on PATH."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def git_uninstalled(self) -> Generator[None, None, None]:
        """Simulate a machine with no git.

        Both patches are needed: the FileNotFoundError is what subprocess raises for a missing
        executable, and the empty which() is how git_utils tells that apart from a missing cwd.
        """
        with (
            patch("griptape_nodes.utils.git_utils.subprocess.run", side_effect=FileNotFoundError),
            patch("griptape_nodes.utils.git_utils.shutil.which", return_value=None),
        ):
            yield

    @pytest.mark.usefixtures("git_uninstalled")
    def test_reader_raises_git_not_found_error_naming_git_as_a_requirement(self, temp_dir: Path) -> None:
        """Test that get_current_tag raises GitNotFoundError naming git as a requirement when git is missing."""
        # A bare ".git" marker is enough for is_git_repository's filesystem check; no real
        # git invocation happens before the patched subprocess.run call.
        (temp_dir / ".git").mkdir()

        with pytest.raises(GitNotFoundError, match="git was not found on PATH"):
            get_current_tag(temp_dir)

    @pytest.mark.usefixtures("git_uninstalled")
    def test_mutator_raises_git_not_found_error_naming_git_as_a_requirement(self) -> None:
        """Test that remote_ref_exists raises GitNotFoundError naming git as a requirement when git is missing."""
        with pytest.raises(GitNotFoundError, match="git was not found on PATH"):
            remote_ref_exists("https://example.com/user/repo.git", "main")

    @pytest.mark.usefixtures("git_uninstalled")
    def test_git_not_found_error_is_catchable_as_git_error(self) -> None:
        """Test that callers handling the base GitError also handle a missing git."""
        with pytest.raises(GitError):
            remote_ref_exists("https://example.com/user/repo.git", "main")

    @pytest.mark.usefixtures("git_uninstalled")
    def test_get_git_info_reports_no_details_when_git_is_missing(self, temp_dir: Path) -> None:
        """Test that get_git_info degrades instead of raising, so a library still loads without git.

        This runs on every metadata load, where git details are informational.
        """
        (temp_dir / ".git").mkdir()

        git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote is None
        assert git_ref is None


class TestVanishedRepository:
    """Test behavior when a repository directory is deleted out from under a git call.

    subprocess reports a missing executable and a missing working directory with the same
    exception, so these pin down that a deleted folder is never misreported as a missing git.
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_mutator_names_the_missing_folder_rather_than_blaming_git(self, temp_dir: Path) -> None:
        """Test that a mutator reports the deleted folder, not a missing git installation."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")
        # Pass is_git_repository's filesystem check, then delete the directory so the git call
        # itself is the thing that finds it gone.
        with patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True):
            shutil.rmtree(clone)

            with pytest.raises(GitRepositoryError, match="no longer there"):
                has_uncommitted_changes(clone)

    def test_accessor_reports_no_answer_rather_than_raising(self, temp_dir: Path) -> None:
        """Test that a query accessor treats a deleted folder as "no answer", as it does a detached HEAD."""
        origin = make_origin_repo(temp_dir / "origin")
        clone = clone_repo(origin, temp_dir / "clone")

        with patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True):
            shutil.rmtree(clone)

            assert get_git_remote(clone) is None
            assert get_git_info(clone) == (None, None)


class TestParseCommitDatetime:
    """Test parse_commit_datetime function."""

    def test_parses_iso_8601_with_offset(self) -> None:
        result = parse_commit_datetime("2024-01-15T12:30:00+00:00")

        assert result == datetime(2024, 1, 15, 12, 30, 0, tzinfo=UTC)
        assert result is not None
        assert result.tzinfo is not None

    def test_parses_iso_8601_with_non_utc_offset(self) -> None:
        result = parse_commit_datetime("2024-01-15T12:30:00-05:00")

        assert result is not None
        # Same instant, expressed in UTC.
        assert result.astimezone(UTC) == datetime(2024, 1, 15, 17, 30, 0, tzinfo=UTC)

    def test_assumes_utc_for_naive_timestamp(self) -> None:
        result = parse_commit_datetime("2024-01-15T12:30:00")

        assert result == datetime(2024, 1, 15, 12, 30, 0, tzinfo=UTC)

    def test_strips_surrounding_whitespace(self) -> None:
        result = parse_commit_datetime("  2024-01-15T12:30:00+00:00\n")

        assert result == datetime(2024, 1, 15, 12, 30, 0, tzinfo=UTC)

    def test_returns_none_for_empty_string(self) -> None:
        assert parse_commit_datetime("") is None
        assert parse_commit_datetime("   ") is None

    def test_returns_none_for_unparsable_string(self) -> None:
        assert parse_commit_datetime("not-a-date") is None
