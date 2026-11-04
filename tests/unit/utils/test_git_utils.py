"""Unit tests for git_utils functions that never run git."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from griptape_nodes.utils.git_utils import (
    extract_repo_name_from_url,
    is_git_repository,
    is_git_url,
    normalize_github_url,
    parse_commit_datetime,
    parse_git_url_with_ref,
)


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

    def test_parse_git_url_with_ref_keeps_userinfo_when_no_ref(self) -> None:
        """Test that user@host userinfo is not mistaken for a ref."""
        url, ref = parse_git_url_with_ref("https://token@github.com/user/repo.git")

        assert url == "https://token@github.com/user/repo.git"
        assert ref is None

    def test_parse_git_url_with_ref_splits_ref_after_userinfo(self) -> None:
        """Test that a ref is split off a URL that also has userinfo."""
        url, ref = parse_git_url_with_ref("https://token@github.com/user/repo.git@stable")

        assert url == "https://token@github.com/user/repo.git"
        assert ref == "stable"

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
