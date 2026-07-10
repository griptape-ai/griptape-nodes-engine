"""Unit tests for git_utils module."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pygit2
import pytest

from griptape_nodes.utils.git_utils import (
    GitCloneError,
    GitPullError,
    GitRefError,
    GitRemoteError,
    GitRepositoryError,
    _CredentialCallbacks,
    clone_repository,
    extract_repo_name_from_url,
    get_current_ref,
    get_git_info,
    get_git_remote,
    get_git_repository_root,
    git_update_from_remote,
    is_git_repository,
    is_git_url,
    normalize_github_url,
    parse_commit_datetime,
    parse_git_url_with_ref,
    remote_ref_exists,
    switch_branch,
)

if TYPE_CHECKING:
    from collections.abc import Generator


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
        """Test that local paths are not recognized as git URLs."""
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
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_discover.side_effect = pygit2.GitError("not a git repository")

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
        with patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git:
            mock_is_git.return_value = False

            result = get_git_remote(temp_dir)

            assert result is None

    def test_get_git_remote_returns_none_when_repository_not_discovered(self, temp_dir: Path) -> None:
        """Test that None is returned when repository cannot be discovered."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = None

            result = get_git_remote(temp_dir)

            assert result is None

    def test_get_git_remote_returns_none_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that None is returned when no origin remote exists."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_repo = Mock()
            mock_repo.remotes = {}
            mock_repo_class.return_value = mock_repo

            result = get_git_remote(temp_dir)

            assert result is None

    def test_get_git_remote_returns_url_when_origin_exists(self, temp_dir: Path) -> None:
        """Test that remote URL is returned when origin exists."""
        expected_url = "https://github.com/user/repo.git"

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_remote = Mock()
            mock_remote.url = expected_url
            mock_repo = Mock()
            mock_repo.remotes = {"origin": mock_remote}
            mock_repo_class.return_value = mock_repo

            result = get_git_remote(temp_dir)

            assert result == expected_url

    def test_get_git_remote_raises_error_on_git_error(self, temp_dir: Path) -> None:
        """Test that GitRemoteError is raised on pygit2.GitError."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.side_effect = pygit2.GitError("error")

            with pytest.raises(GitRemoteError) as exc_info:
                get_git_remote(temp_dir)

            assert "Error getting git remote" in str(exc_info.value)


class TestGetCurrentBranch:
    """Test get_current_ref function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_current_ref_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        with patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git:
            mock_is_git.return_value = False

            result = get_current_ref(temp_dir)

            assert result is None

    def test_get_current_ref_returns_none_when_repository_not_discovered(self, temp_dir: Path) -> None:
        """Test that None is returned when repository cannot be discovered."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = None

            result = get_current_ref(temp_dir)

            assert result is None

    def test_get_current_ref_returns_none_when_head_detached(self, temp_dir: Path) -> None:
        """Test that commit SHA is returned when HEAD is detached."""
        expected_commit_sha = "abc123def456"

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
            patch("griptape_nodes.utils.git_utils.get_current_tag") as mock_get_current_tag,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")
            mock_get_current_tag.return_value = None

            mock_head = Mock()
            mock_head.target = expected_commit_sha
            mock_repo = Mock()
            mock_repo.head_is_unborn = False
            mock_repo.head_is_detached = True
            mock_repo.head = mock_head
            mock_repo_class.return_value = mock_repo

            result = get_current_ref(temp_dir)

            assert result == expected_commit_sha

    def test_get_current_ref_returns_branch_name_when_on_branch(self, temp_dir: Path) -> None:
        """Test that branch name is returned when on a branch."""
        expected_branch = "main"

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_head = Mock()
            mock_head.shorthand = expected_branch
            mock_repo = Mock()
            mock_repo.head_is_unborn = False
            mock_repo.head_is_detached = False
            mock_repo.head = mock_head
            mock_repo_class.return_value = mock_repo

            result = get_current_ref(temp_dir)

            assert result == expected_branch

    def test_get_current_ref_raises_error_on_git_error(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised on pygit2.GitError."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.side_effect = pygit2.GitError("error")

            with pytest.raises(GitRefError) as exc_info:
                get_current_ref(temp_dir)

            assert "Error getting current git reference" in str(exc_info.value)


class TestGetGitRepositoryRoot:
    """Test get_git_repository_root function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_get_git_repository_root_returns_none_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that None is returned when path is not a git repository."""
        with patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git:
            mock_is_git.return_value = False

            result = get_git_repository_root(temp_dir)

            assert result is None

    def test_get_git_repository_root_returns_none_when_repository_not_discovered(self, temp_dir: Path) -> None:
        """Test that None is returned when repository cannot be discovered."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = None

            result = get_git_repository_root(temp_dir)

            assert result is None

    def test_get_git_repository_root_returns_parent_for_normal_repository(self, temp_dir: Path) -> None:
        """Test that parent of .git directory is returned for normal repository."""
        git_dir = temp_dir / ".git"

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(git_dir)

            result = get_git_repository_root(temp_dir)

            assert result == temp_dir

    def test_get_git_repository_root_returns_bare_repo_path_for_bare_repository(self, temp_dir: Path) -> None:
        """Test that bare repository path is returned for bare repositories."""
        bare_repo_path = temp_dir / "repo.git"
        bare_repo_path.mkdir()

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(bare_repo_path)

            mock_repo = Mock()
            mock_repo.is_bare = True
            mock_repo_class.return_value = mock_repo

            result = get_git_repository_root(temp_dir)

            assert result == bare_repo_path

    def test_get_git_repository_root_raises_error_on_git_error(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised on pygit2.GitError."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.side_effect = pygit2.GitError("error")

            with pytest.raises(GitRepositoryError) as exc_info:
                get_git_repository_root(temp_dir)

            assert "Error getting git repository root" in str(exc_info.value)


class TestGitUpdateFromRemote:
    """Test git_update_from_remote function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_git_update_from_remote_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git:
            mock_is_git.return_value = False

            with pytest.raises(GitRepositoryError) as exc_info:
                git_update_from_remote(temp_dir)

            assert "not a git repository" in str(exc_info.value)

    def test_git_update_from_remote_raises_error_when_repository_not_discovered(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when repository cannot be discovered."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = None

            with pytest.raises(GitRepositoryError) as exc_info:
                git_update_from_remote(temp_dir)

            assert "Cannot discover repository" in str(exc_info.value)

    def test_git_update_from_remote_raises_error_when_head_detached(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when HEAD is detached."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_repo = Mock()
            mock_repo.head_is_detached = True
            mock_repo_class.return_value = mock_repo

            with pytest.raises(GitPullError) as exc_info:
                git_update_from_remote(temp_dir)

            assert "detached HEAD" in str(exc_info.value)

    def test_git_update_from_remote_raises_error_when_no_upstream_branch(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when no upstream branch is set."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_branch = Mock()
            mock_branch.upstream = None
            mock_branch.branch_name = "main"

            mock_head = Mock()
            mock_head.shorthand = "main"

            mock_branches = Mock()
            mock_branches.get.return_value = mock_branch

            mock_repo = Mock()
            mock_repo.head_is_detached = False
            mock_repo.head = mock_head
            mock_repo.branches = mock_branches
            mock_repo_class.return_value = mock_repo

            with pytest.raises(GitPullError) as exc_info:
                git_update_from_remote(temp_dir)

            assert "No upstream branch" in str(exc_info.value)

    def test_git_update_from_remote_raises_error_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when no origin remote exists."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_branch = Mock()
            mock_branch.upstream = Mock()
            mock_branch.branch_name = "main"

            mock_head = Mock()
            mock_head.shorthand = "main"

            mock_branches = Mock()
            mock_branches.get.return_value = mock_branch

            mock_repo = Mock()
            mock_repo.head_is_detached = False
            mock_repo.head = mock_head
            mock_repo.branches = mock_branches
            mock_repo.remotes = {}
            mock_repo_class.return_value = mock_repo

            with pytest.raises(GitPullError) as exc_info:
                git_update_from_remote(temp_dir)

            assert "No origin remote" in str(exc_info.value)

    def test_git_update_from_remote_succeeds_with_pygit2(self, temp_dir: Path) -> None:
        """Test that function succeeds when pygit2 operations succeed."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
            patch("griptape_nodes.utils.git_utils.has_uncommitted_changes") as mock_has_changes,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")
            mock_has_changes.return_value = False

            mock_upstream_ref = Mock()
            mock_upstream_ref.target = "abc123"

            mock_references = Mock()
            mock_references.get.return_value = mock_upstream_ref

            mock_branch = Mock()
            mock_branch.upstream = Mock()
            mock_branch.upstream.branch_name = "origin/main"

            mock_head = Mock()
            mock_head.shorthand = "main"

            mock_branches = Mock()
            mock_branches.get.return_value = mock_branch

            mock_remote = Mock()
            mock_repo = Mock()
            mock_repo.head_is_detached = False
            mock_repo.head = mock_head
            mock_repo.branches = mock_branches
            mock_repo.remotes = {"origin": mock_remote}
            mock_repo.references = mock_references
            mock_repo_class.return_value = mock_repo

            git_update_from_remote(temp_dir)

            mock_remote.fetch.assert_called_once()
            mock_repo.reset.assert_called_once()

    def test_git_update_from_remote_raises_error_when_fetch_fails(self, temp_dir: Path) -> None:
        """Test that GitPullError is raised when fetch fails."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
            patch("griptape_nodes.utils.git_utils.has_uncommitted_changes") as mock_has_changes,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")
            mock_has_changes.return_value = False

            mock_branch = Mock()
            mock_branch.upstream = Mock()
            mock_branch.upstream.branch_name = "origin/main"

            mock_head = Mock()
            mock_head.shorthand = "main"

            mock_branches = Mock()
            mock_branches.get.return_value = mock_branch

            mock_remote = Mock()
            mock_remote.fetch.side_effect = pygit2.GitError("fetch failed")
            mock_repo = Mock()
            mock_repo.head_is_detached = False
            mock_repo.head = mock_head
            mock_repo.branches = mock_branches
            mock_repo.remotes = {"origin": mock_remote}
            mock_repo_class.return_value = mock_repo

            with pytest.raises(GitPullError) as exc_info:
                git_update_from_remote(temp_dir)

            assert "Git error during update" in str(exc_info.value)


class TestSwitchBranch:
    """Test switch_branch function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_switch_branch_raises_error_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that GitRepositoryError is raised when not a git repository."""
        with patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git:
            mock_is_git.return_value = False

            with pytest.raises(GitRepositoryError) as exc_info:
                switch_branch(temp_dir, "main")

            assert "not a git repository" in str(exc_info.value)

    def test_switch_branch_raises_error_when_no_origin_remote(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised when no origin remote exists."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_repo = Mock()
            mock_repo.remotes = {}
            mock_repo_class.return_value = mock_repo

            with pytest.raises(GitRefError) as exc_info:
                switch_branch(temp_dir, "main")

            assert "No origin remote" in str(exc_info.value)

    def test_switch_branch_checks_out_existing_local_branch(self, temp_dir: Path) -> None:
        """Test that existing local branch is checked out."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_branch = Mock()
            mock_branches = Mock()
            mock_branches.get.return_value = mock_branch

            mock_remote = Mock()
            mock_repo = Mock()
            mock_repo.remotes = {"origin": mock_remote}
            mock_repo.branches = mock_branches
            mock_repo_class.return_value = mock_repo

            switch_branch(temp_dir, "main")

            mock_remote.fetch.assert_called_once()
            mock_repo.checkout.assert_called_once_with(mock_branch)

    def test_switch_branch_creates_tracking_branch_from_remote(self, temp_dir: Path) -> None:
        """Test that tracking branch is created from remote when local doesn't exist."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_remote_branch = Mock()
            mock_remote_branch.target = "abc123"
            mock_commit = Mock()
            mock_new_branch = Mock()

            mock_local_branches = Mock()
            mock_local_branches.create.return_value = mock_new_branch

            mock_branches = Mock()
            # First call for local branch returns None, second for remote returns mock
            mock_branches.get.side_effect = [None, mock_remote_branch]
            mock_branches.local = mock_local_branches

            mock_remote = Mock()
            mock_repo = Mock()
            mock_repo.remotes = {"origin": mock_remote}
            mock_repo.branches = mock_branches
            mock_repo.get.return_value = mock_commit
            mock_repo_class.return_value = mock_repo

            switch_branch(temp_dir, "feature")

            mock_remote.fetch.assert_called_once()
            mock_local_branches.create.assert_called_once_with("feature", mock_commit)
            assert mock_new_branch.upstream == mock_remote_branch
            mock_repo.checkout.assert_called_once_with(mock_new_branch)

    def test_switch_branch_raises_error_when_branch_not_found(self, temp_dir: Path) -> None:
        """Test that GitRefError is raised when branch doesn't exist locally or remotely."""
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository") as mock_is_git,
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository") as mock_discover,
            patch("griptape_nodes.utils.git_utils.pygit2.Repository") as mock_repo_class,
        ):
            mock_is_git.return_value = True
            mock_discover.return_value = str(temp_dir / ".git")

            mock_branches = Mock()
            mock_branches.get.return_value = None

            mock_remote = Mock()
            mock_repo = Mock()
            mock_repo.remotes = {"origin": mock_remote}
            mock_repo.branches = mock_branches
            mock_repo_class.return_value = mock_repo

            with pytest.raises(GitRefError) as exc_info:
                switch_branch(temp_dir, "nonexistent")

            assert "not found" in str(exc_info.value)


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

        with pytest.raises(GitCloneError) as exc_info:
            clone_repository("https://github.com/user/repo.git", existing_path)

        assert "already exists" in str(exc_info.value)

    def test_clone_repository_clones_https_url(self, temp_dir: Path) -> None:
        """Test that HTTPS URLs are cloned successfully."""
        target_path = temp_dir / "repo"

        with patch("griptape_nodes.utils.git_utils.pygit2.clone_repository") as mock_clone:
            mock_clone.return_value = Mock()

            clone_repository("https://github.com/user/repo.git", target_path)

            args, kwargs = mock_clone.call_args
            assert args == ("https://github.com/user/repo.git", str(target_path))
            assert isinstance(kwargs["callbacks"], _CredentialCallbacks)

    def test_clone_repository_raises_error_when_clone_returns_none(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised when clone returns None."""
        target_path = temp_dir / "repo"

        with patch("griptape_nodes.utils.git_utils.pygit2.clone_repository") as mock_clone:
            mock_clone.return_value = None

            with pytest.raises(GitCloneError) as exc_info:
                clone_repository("https://github.com/user/repo.git", target_path)

            assert "Failed to clone" in str(exc_info.value)

    def test_clone_repository_checks_out_specified_branch(self, temp_dir: Path) -> None:
        """Test that specified branch is checked out after cloning."""
        target_path = temp_dir / "repo"

        with patch("griptape_nodes.utils.git_utils.pygit2.clone_repository") as mock_clone:
            mock_branch = Mock()
            mock_repo = Mock()
            mock_repo.branches = {"feature": mock_branch}
            mock_clone.return_value = mock_repo

            clone_repository("https://github.com/user/repo.git", target_path, "feature")

            mock_repo.checkout.assert_called_once_with(mock_branch)

    def test_clone_repository_checks_out_commit_when_branch_not_found(self, temp_dir: Path) -> None:
        """Test that commit is checked out when branch doesn't exist."""
        target_path = temp_dir / "repo"

        with patch("griptape_nodes.utils.git_utils.pygit2.clone_repository") as mock_clone:
            mock_commit = Mock()
            mock_commit.id = "abc123"
            mock_repo = Mock()
            mock_repo.branches = {}
            mock_repo.references = []
            mock_repo.revparse_single.return_value = mock_commit
            mock_clone.return_value = mock_repo

            clone_repository("https://github.com/user/repo.git", target_path, "abc123")

            mock_repo.checkout_tree.assert_called_once_with(mock_commit)
            mock_repo.set_head.assert_called_once_with(mock_commit.id)

    def test_clone_repository_raises_error_on_git_error(self, temp_dir: Path) -> None:
        """Test that GitCloneError is raised on pygit2.GitError."""
        target_path = temp_dir / "repo"

        with patch("griptape_nodes.utils.git_utils.pygit2.clone_repository") as mock_clone:
            mock_clone.side_effect = pygit2.GitError("clone failed")

            with pytest.raises(GitCloneError) as exc_info:
                clone_repository("https://github.com/user/repo.git", target_path)

            assert "Git error while cloning" in str(exc_info.value)


class TestGetGitInfo:
    """Test get_git_info function.

    get_git_info opens the pygit2 repo once and returns both (git_remote, git_ref),
    replacing the previous pattern of calling get_git_remote + get_current_ref
    separately (which opened the repo 2-3x per library).
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def _make_repo_mock(  # noqa: PLR0913
        self,
        *,
        remote_url: str | None = "https://github.com/user/repo.git",
        head_is_unborn: bool = False,
        head_is_detached: bool = False,
        branch_shorthand: str = "main",
        head_target: object = "abc123",
        references: dict | None = None,
    ) -> Mock:
        mock_repo = Mock()
        mock_repo.remotes = {"origin": Mock(url=remote_url)} if remote_url else {}
        mock_repo.head_is_unborn = head_is_unborn
        mock_repo.head_is_detached = head_is_detached
        mock_repo.head = Mock(shorthand=branch_shorthand, target=head_target)
        mock_repo.references = references if references is not None else {}
        return mock_repo

    def test_returns_none_none_when_not_git_repository(self, temp_dir: Path) -> None:
        with patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=False):
            git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote is None
        assert git_ref is None

    def test_returns_none_none_when_repository_not_discovered(self, temp_dir: Path) -> None:
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=None),
        ):
            git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote is None
        assert git_ref is None

    def test_returns_none_none_on_git_error_opening_repo(self, temp_dir: Path) -> None:
        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch(
                "griptape_nodes.utils.git_utils.pygit2.discover_repository",
                side_effect=pygit2.GitError("error"),
            ),
        ):
            git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote is None
        assert git_ref is None

    def test_returns_remote_and_branch_when_on_branch(self, temp_dir: Path) -> None:
        mock_repo = self._make_repo_mock(remote_url="https://github.com/user/repo.git", branch_shorthand="main")

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=str(temp_dir / ".git")),
            patch("griptape_nodes.utils.git_utils.pygit2.Repository", return_value=mock_repo),
        ):
            git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote == "https://github.com/user/repo.git"
        assert git_ref == "main"

    def test_returns_none_remote_when_no_origin(self, temp_dir: Path) -> None:
        mock_repo = self._make_repo_mock(remote_url=None, branch_shorthand="main")

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=str(temp_dir / ".git")),
            patch("griptape_nodes.utils.git_utils.pygit2.Repository", return_value=mock_repo),
        ):
            git_remote, git_ref = get_git_info(temp_dir)

        assert git_remote is None
        assert git_ref == "main"

    def test_returns_commit_sha_when_head_detached_and_no_tag(self, temp_dir: Path) -> None:
        expected_sha = "deadbeef1234"
        mock_repo = self._make_repo_mock(
            remote_url=None, head_is_detached=True, head_target=expected_sha, references={}
        )

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=str(temp_dir / ".git")),
            patch("griptape_nodes.utils.git_utils.pygit2.Repository", return_value=mock_repo),
        ):
            _git_remote, git_ref = get_git_info(temp_dir)

        assert git_ref == expected_sha

    def test_returns_tag_name_when_head_on_tag(self, temp_dir: Path) -> None:
        expected_sha = "deadbeef1234"
        expected_tag = "v1.0.0"

        mock_tag_ref = Mock()
        mock_tag_ref.peel.return_value.id = expected_sha

        mock_repo = self._make_repo_mock(
            remote_url=None,
            head_is_detached=True,
            head_target=expected_sha,
            references={f"refs/tags/{expected_tag}": mock_tag_ref},
        )

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=str(temp_dir / ".git")),
            patch("griptape_nodes.utils.git_utils.pygit2.Repository", return_value=mock_repo),
        ):
            _git_remote, git_ref = get_git_info(temp_dir)

        assert git_ref == expected_tag

    def test_returns_none_ref_when_head_unborn(self, temp_dir: Path) -> None:
        mock_repo = self._make_repo_mock(remote_url=None, head_is_unborn=True)

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=str(temp_dir / ".git")),
            patch("griptape_nodes.utils.git_utils.pygit2.Repository", return_value=mock_repo),
        ):
            _git_remote, git_ref = get_git_info(temp_dir)

        assert git_ref is None

    def test_opens_repo_exactly_once(self, temp_dir: Path) -> None:
        mock_repo = self._make_repo_mock()

        with (
            patch("griptape_nodes.utils.git_utils.is_git_repository", return_value=True),
            patch("griptape_nodes.utils.git_utils.pygit2.discover_repository", return_value=str(temp_dir / ".git")),
            patch("griptape_nodes.utils.git_utils.pygit2.Repository", return_value=mock_repo) as mock_repo_class,
        ):
            get_git_info(temp_dir)

        mock_repo_class.assert_called_once()


class TestRemoteRefExists:
    """Tests for remote_ref_exists."""

    def test_returns_true_when_git_cli_lists_matching_ref(self) -> None:
        completed = Mock(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        with (
            patch("griptape_nodes.utils.git_utils._is_git_available", return_value=True),
            patch("griptape_nodes.utils.git_utils.subprocess.run", return_value=completed) as mock_run,
        ):
            assert remote_ref_exists("git@github.com:owner/repo.git", "main") is True

        args = mock_run.call_args.args[0]
        assert args == ["git", "ls-remote", "--heads", "--tags", "git@github.com:owner/repo.git", "main"]

    def test_returns_false_when_git_cli_lists_no_matching_ref(self) -> None:
        completed = Mock(returncode=0, stdout="\n", stderr="")
        with (
            patch("griptape_nodes.utils.git_utils._is_git_available", return_value=True),
            patch("griptape_nodes.utils.git_utils.subprocess.run", return_value=completed),
        ):
            assert remote_ref_exists("git@github.com:owner/repo.git", "local-only-branch") is False

    def test_raises_git_remote_error_when_git_cli_fails(self) -> None:
        completed = Mock(returncode=128, stdout="", stderr="fatal: could not read from remote\n")
        with (
            patch("griptape_nodes.utils.git_utils._is_git_available", return_value=True),
            patch("griptape_nodes.utils.git_utils.subprocess.run", return_value=completed),
            pytest.raises(GitRemoteError, match="Failed to query remote refs"),
        ):
            remote_ref_exists("git@github.com:owner/repo.git", "main")

    def test_returns_true_when_pygit2_lists_matching_ref(self) -> None:
        mock_remote = Mock()
        mock_remote.ls_remotes.return_value = [{"name": "refs/heads/feature/foo"}, {"name": "refs/heads/main"}]
        mock_repo = Mock()
        mock_repo.remotes.create.return_value = mock_remote
        with (
            patch("griptape_nodes.utils.git_utils._is_git_available", return_value=False),
            patch("griptape_nodes.utils.git_utils.pygit2.init_repository", return_value=mock_repo),
        ):
            assert remote_ref_exists("https://github.com/owner/repo.git", "feature/foo") is True

    def test_returns_false_when_pygit2_lists_no_matching_ref(self) -> None:
        mock_remote = Mock()
        mock_remote.ls_remotes.return_value = [{"name": "refs/heads/main"}]
        mock_repo = Mock()
        mock_repo.remotes.create.return_value = mock_remote
        with (
            patch("griptape_nodes.utils.git_utils._is_git_available", return_value=False),
            patch("griptape_nodes.utils.git_utils.pygit2.init_repository", return_value=mock_repo),
        ):
            assert remote_ref_exists("https://github.com/owner/repo.git", "local-only-branch") is False

    def test_raises_git_remote_error_when_pygit2_fails(self) -> None:
        mock_repo = Mock()
        mock_repo.remotes.create.side_effect = pygit2.GitError("auth failed")
        with (
            patch("griptape_nodes.utils.git_utils._is_git_available", return_value=False),
            patch("griptape_nodes.utils.git_utils.pygit2.init_repository", return_value=mock_repo),
            pytest.raises(GitRemoteError, match="Failed to query remote refs"),
        ):
            remote_ref_exists("https://github.com/owner/repo.git", "main")


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
