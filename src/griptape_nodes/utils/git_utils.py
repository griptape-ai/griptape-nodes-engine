"""Git utilities for library updates."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from griptape_nodes.utils.file_utils import find_file_in_directory

logger = logging.getLogger("griptape_nodes")


class GitError(Exception):
    """Base exception for git operations."""


class GitNotFoundError(GitError):
    """Raised when the git executable is not available on PATH.

    Subclasses GitError rather than any of the operation-specific errors below: a
    missing git is a fault of the environment, not of the repository, remote, or
    ref the caller happened to be working with.
    """


class GitRepositoryError(GitError):
    """Raised when a path is not a valid git repository."""


class GitRemoteError(GitError):
    """Raised when git remote operations fail."""


class GitRefError(GitError):
    """Raised when git ref operations fail."""


class GitCloneError(GitError):
    """Raised when git clone operations fail."""


class GitPullError(GitError):
    """Raised when git pull operations fail."""


class GitUrlWithRef(NamedTuple):
    """Parsed git URL with optional ref (branch/tag/commit)."""

    url: str
    ref: str | None


class LibraryJsonCheckout(NamedTuple):
    """Result of fetching a library's JSON metadata from a git remote.

    ``commit_datetime`` is the timezone-aware timestamp of the checked-out commit, or None when
    it could not be determined.
    """

    library_version: str
    commit_sha: str
    commit_datetime: datetime | None
    library_data: dict


def parse_commit_datetime(iso_string: str) -> datetime | None:
    """Parse a git commit timestamp in strict ISO 8601 form into a timezone-aware datetime.

    Args:
        iso_string: The commit timestamp string (e.g. from ``git log --format=%cI``).

    Returns:
        A timezone-aware datetime, or None if the string is empty or cannot be parsed. Naive
        timestamps are assumed to be UTC.
    """
    iso_string = iso_string.strip()
    if not iso_string:
        return None

    try:
        parsed = datetime.fromisoformat(iso_string)
    except ValueError:
        logger.debug("Failed to parse git commit datetime %r", iso_string)
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def is_git_url(url: str) -> bool:
    """Check if a string is a git URL.

    Args:
        url: The URL to check.

    Returns:
        bool: True if the string is a git URL, False otherwise.
    """
    git_url_patterns = (
        "http://",
        "https://",
        "git://",
        "ssh://",
        "git@",
    )
    return url.startswith(git_url_patterns)


def parse_git_url_with_ref(url_with_ref: str) -> GitUrlWithRef:
    """Parse a git URL that may contain a ref specification using @ delimiter.

    Supports format: url@ref where ref can be a branch, tag, or commit SHA.
    If no @ delimiter is present, returns the URL with None as the ref.

    Args:
        url_with_ref: A git URL optionally followed by @ref
            (e.g., "https://github.com/user/repo@stable" or "user/repo@v1.0.0")

    Returns:
        GitUrlWithRef: Parsed URL with optional ref (branch/tag/commit).

    Examples:
        "https://github.com/user/repo@stable" -> GitUrlWithRef("https://github.com/user/repo", "stable")
        "user/repo@main" -> GitUrlWithRef("user/repo", "main")
        "https://github.com/user/repo" -> GitUrlWithRef("https://github.com/user/repo", None)
        "user/repo" -> GitUrlWithRef("user/repo", None)
    """
    url_with_ref = url_with_ref.strip()

    # Check for @ delimiter (but not in SSH URLs like git@github.com)
    # We need to be careful not to split on the @ in git@github.com
    if url_with_ref.startswith("git@"):
        # SSH URL format - look for @ after the domain
        # Format: git@github.com:user/repo@ref
        parts = url_with_ref.split(":", 1)
        if len(parts) == 2 and "@" in parts[1]:  # noqa: PLR2004
            # Split the path part only
            path_parts = parts[1].rsplit("@", 1)
            if len(path_parts) == 2:  # noqa: PLR2004
                return GitUrlWithRef(url=f"{parts[0]}:{path_parts[0]}", ref=path_parts[1])
        return GitUrlWithRef(url=url_with_ref, ref=None)

    # For HTTPS/HTTP URLs and shorthand, split on last @
    if "@" in url_with_ref:
        # Use rsplit to split from the right, so we get the last @ (in case of user:pass@host format)
        parts = url_with_ref.rsplit("@", 1)
        if len(parts) == 2:  # noqa: PLR2004
            return GitUrlWithRef(url=parts[0], ref=parts[1])

    return GitUrlWithRef(url=url_with_ref, ref=None)


def _is_github_https_url(url: str) -> bool:
    """Return True if the URL is an HTTP(S) URL whose hostname is github.com."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname == "github.com"


def normalize_github_url(url_or_shorthand: str) -> str:
    """Normalize a GitHub URL or shorthand to a full HTTPS git URL.

    Converts GitHub shorthand (e.g., "owner/repo") to full HTTPS URLs.
    Ensures .git suffix on GitHub URLs. Passes through non-GitHub URLs unchanged.
    Preserves @ref suffix if present.

    Args:
        url_or_shorthand: Either a full git URL or GitHub shorthand (e.g., "user/repo"),
            optionally with @ref suffix (e.g., "user/repo@stable").

    Returns:
        A normalized HTTPS git URL, preserving any @ref suffix.

    Examples:
        "griptape-ai/griptape-nodes-library-topazlabs" -> "https://github.com/griptape-ai/griptape-nodes-library-topazlabs.git"
        "griptape-ai/repo@stable" -> "https://github.com/griptape-ai/repo.git@stable"
        "https://github.com/user/repo" -> "https://github.com/user/repo.git"
        "https://github.com/user/repo@main" -> "https://github.com/user/repo.git@main"
        "git@github.com:user/repo.git" -> "git@github.com:user/repo.git"
        "https://gitlab.com/user/repo" -> "https://gitlab.com/user/repo"
    """
    url_or_shorthand = url_or_shorthand.strip().rstrip("/")

    # Parse out @ref suffix if present
    url, ref = parse_git_url_with_ref(url_or_shorthand)

    # Check if it's GitHub shorthand: owner/repo (no protocol, single slash, no domain)
    if not is_git_url(url) and "/" in url and url.count("/") == 1:
        # Assume GitHub shorthand
        normalized = f"https://github.com/{url}.git"
    elif _is_github_https_url(url) and not url.endswith(".git"):
        # If it's an HTTPS GitHub URL, ensure .git suffix
        normalized = f"{url}.git"
    else:
        # Pass through all other URLs unchanged
        normalized = url

    # Re-append @ref suffix if it was present
    if ref is not None:
        return f"{normalized}@{ref}"

    return normalized


def extract_repo_name_from_url(url: str) -> str:
    """Extract the repository name from a git URL.

    Handles URLs with @ref suffix by stripping the ref before extraction.

    Args:
        url: A git URL (HTTPS, SSH, or GitHub shorthand), optionally with @ref suffix.

    Returns:
        The repository name without the .git suffix or @ref.

    Examples:
        "https://github.com/griptape-ai/griptape-nodes-library-advanced" -> "griptape-nodes-library-advanced"
        "https://github.com/griptape-ai/griptape-nodes-library-advanced.git" -> "griptape-nodes-library-advanced"
        "https://github.com/griptape-ai/griptape-nodes-library-advanced@stable" -> "griptape-nodes-library-advanced"
        "git@github.com:user/repo.git" -> "repo"
        "griptape-ai/repo" -> "repo"
        "griptape-ai/repo@main" -> "repo"
    """
    url = url.strip()

    # Strip @ref suffix first, then trailing slashes: a slash right before the ref
    # (e.g. "owner/repo/@ref") would otherwise survive and leave an empty repo name.
    url, _ = parse_git_url_with_ref(url)
    url = url.rstrip("/")

    # Remove .git suffix if present
    url = url.removesuffix(".git")

    # Extract the last part of the path
    # Handle both https://domain/owner/repo and git@domain:owner/repo formats
    if ":" in url and not url.startswith(("http://", "https://", "ssh://")):
        # SSH format: git@github.com:owner/repo
        repo_name = url.split(":")[-1].split("/")[-1]
    else:
        # HTTPS format or shorthand: https://github.com/owner/repo or owner/repo
        repo_name = url.split("/")[-1]

    return repo_name


def is_git_repository(path: Path) -> bool:
    """Check if a directory or its parent is a git repository.

    This checks both the given path and its parent directory for a .git folder.
    This handles cases where library JSON files are in subdirectories of a git
    repository (e.g., monorepo structures).

    Args:
        path: The directory path to check.

    Returns:
        bool: True if the directory or its parent is a git repository, False otherwise.
    """
    if not path.exists():
        return False
    if not path.is_dir():
        return False

    # Check for .git directory or file in the given path (for git worktrees/submodules)
    git_path = path / ".git"
    if git_path.exists():
        return True

    # Check parent directory for .git
    parent_path = path.parent
    if parent_path != path and parent_path.exists():
        parent_git_path = parent_path / ".git"
        if parent_git_path.exists():
            return True

    return False


_GIT_MISSING_MESSAGE = (
    "git was not found on PATH. Griptape Nodes requires a git installation to install and update libraries."
)

# Transports a library URL is allowed to use. Anything outside this list, notably a
# "<helper>::<url>" spelling that makes git exec a git-remote-<helper> binary, is refused
# before a connection is attempted.
_GIT_ALLOWED_PROTOCOLS = "file:git:http:https:ssh"


@cache
def _log_git_env_override(name: str, inherited: str, default: str) -> None:
    """Report that an inherited environment variable displaced a git hardening default.

    Cached so a launcher that sets one of these permanently produces one line per distinct
    value rather than one line per git invocation.
    """
    logger.warning(
        "Inherited %s=%r from the environment, overriding the Griptape Nodes default of %r. "
        "Library installs and updates will use the inherited setting.",
        name,
        inherited,
        default,
    )


def _git_env() -> dict[str, str]:
    """Build the environment for a git subprocess.

    The engine runs headless, so git must never block on an interactive credential
    prompt nobody can answer. This covers git's own prompts; `_git` closes stdin to stop
    the transports git shells out to (ssh, in particular) from prompting either.

    Library URLs reach git from workflow and request payloads, so the transports git will
    speak are pinned to `_GIT_ALLOWED_PROTOCOLS`. git already refuses the `ext` transport
    by default, but leaves unrecognized ones permitted for a directly invoked command.

    An inherited value wins for both, letting a launcher opt back into prompting or admit
    a transport this list omits. Because that also lets a launcher turn off the transport
    restriction, an override is logged so it is visible rather than silent.
    """
    defaults = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": _GIT_ALLOWED_PROTOCOLS,
    }

    for name, default in defaults.items():
        inherited = os.environ.get(name)
        if inherited is not None and inherited != default:
            _log_git_env_override(name, inherited, default)

    return {**defaults, **os.environ}


def _reject_option_like(value: str, description: str, error_cls: type[GitError]) -> None:
    """Reject a value git's argument parser would read as an option rather than data.

    git refnames cannot begin with "-" and no git URL scheme does either, so a leading
    "-" is always malformed input. Rejecting it keeps a caller-supplied URL or ref from
    reaching git as a flag, where `--upload-pack=<cmd>` would run an arbitrary command.

    Raises:
        error_cls: If the value would be parsed as an option.
    """
    if value.startswith("-"):
        msg = f"Invalid {description}: {value!r} must not start with '-'"
        raise error_cls(msg)


def _spawn_failure_error(cwd: Path | None, error: OSError) -> GitError:
    """Explain why a git subprocess could not be started.

    subprocess reports a missing git, a git that cannot be run, and an unusable working directory
    with different sibling OSError types, so the cause has to be established afterwards from what
    is actually missing. Every outcome is a GitError: callers guard on that, and a raw OS error
    escaping here would reach a request handler unhandled.
    """
    # which() also rejects a git that is present but not executable, which is the same problem
    # from the caller's point of view: there is no git this process can run.
    if shutil.which("git") is None:
        return GitNotFoundError(_GIT_MISSING_MESSAGE)

    if cwd is not None and not cwd.is_dir():
        msg = f"Cannot run git in {cwd}: no folder exists at that path."
        return GitRepositoryError(msg)

    msg = f"Could not start git in {cwd}: {error}"
    return GitError(msg)


def _git(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    """Run a git command to completion without inspecting its exit code.

    Raises:
        GitNotFoundError: If no runnable git is on PATH.
        GitRepositoryError: If cwd is not a directory.
        GitError: If git cannot be started for any other reason.
    """
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # git writes paths, refs, and messages as UTF-8 regardless of the process locale,
            # so decode as UTF-8 rather than letting the platform's preferred encoding decide.
            # errors="replace" keeps an undecodable byte from turning into a UnicodeDecodeError
            # that escapes as something other than a GitError.
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as e:
        raise _spawn_failure_error(cwd, e) from e


def _run_git(
    args: list[str],
    *,
    error_msg: str,
    cwd: Path | None = None,
    error_cls: type[GitError] = GitError,
) -> str:
    """Run a git command and return its stripped stdout.

    Args:
        args: Arguments to pass to git, without the leading "git".
        error_msg: Prefix for the raised exception's message. git's stderr is appended to it.
        cwd: Directory to run the command in.
        error_cls: Exception type to raise when the command fails.

    Returns:
        str: The command's stdout, stripped.

    Raises:
        error_cls: If the command exits non-zero.
        GitNotFoundError: If no runnable git is on PATH.
        GitRepositoryError: If cwd is not a directory.
    """
    result = _git(args, cwd)
    if result.returncode != 0:
        msg = f"{error_msg}: {result.stderr.strip()}"
        raise error_cls(msg)
    return result.stdout.strip()


def _try_git(args: list[str], cwd: Path | None = None) -> str | None:
    """Run a git command and return its stripped stdout, or None if it failed.

    For queries where a non-zero exit is an answer rather than a fault: no upstream is
    configured, HEAD is detached, the ref doesn't exist. A repository that was deleted
    while the query ran belongs in that group too, so it reads as "no answer" rather than
    propagating out of the accessors built on this.

    Raises:
        GitNotFoundError: If no runnable git is on PATH.
    """
    try:
        result = _git(args, cwd)
    except GitRepositoryError:
        logger.debug("git %s found no repository in %s", " ".join(args), cwd)
        return None

    if result.returncode != 0:
        logger.debug("git %s failed in %s: %s", " ".join(args), cwd, result.stderr.strip())
        return None
    return result.stdout.strip()


def _run_git_detached(args: list[str], *, error_msg: str, error_cls: type[GitError] = GitError) -> str:
    """Run a git command that operates on a remote rather than a local repository.

    Runs in an empty directory so the command can't inherit the repository the engine's
    working directory happens to sit inside. git inspects that repository even for work
    that has nothing to do with it, and refuses to run at all when it is broken or owned
    by another user.

    Raises:
        error_cls: If the command exits non-zero.
        GitNotFoundError: If git is not installed.
    """
    with tempfile.TemporaryDirectory() as neutral_dir:
        return _run_git(args, error_msg=error_msg, cwd=Path(neutral_dir), error_cls=error_cls)


def _head_commit_sha(library_path: Path) -> str | None:
    """Full SHA of the commit HEAD points at, or None when HEAD is unborn."""
    return _try_git(["rev-parse", "--verify", "-q", "HEAD"], library_path)


def _current_branch(library_path: Path) -> str | None:
    """Name of the checked-out branch, or None when HEAD is detached.

    Reports a branch name for an unborn HEAD too, since the branch is only
    unresolvable, not absent. Callers that need a commit check
    ``_head_commit_sha`` first.
    """
    return _try_git(["symbolic-ref", "--short", "HEAD"], library_path)


def _tag_at_head(library_path: Path) -> str | None:
    """Name of a tag pointing at HEAD, or None when HEAD isn't tagged.

    Reports the first name git lists when several tags share the commit.
    """
    tags = _try_git(["tag", "--points-at", "HEAD"], library_path)
    if not tags:
        return None
    return tags.splitlines()[0].strip()


def _ref_exists(library_path: Path, ref: str) -> bool:
    """Whether a fully-qualified ref (e.g. "refs/tags/v1") exists in the repository."""
    return _try_git(["rev-parse", "--verify", "-q", ref], library_path) is not None


def _remote_url(library_path: Path) -> str | None:
    """URL of the origin remote, or None when no origin is configured."""
    return _try_git(["remote", "get-url", "origin"], library_path)


def _upstream_ref(library_path: Path) -> str | None:
    """Upstream of the current branch as "origin/main", or None when unset."""
    return _try_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], library_path)


def _describe_head(library_path: Path) -> str | None:
    """Describe what HEAD points at: branch name, else tag name, else commit SHA.

    Returns None when HEAD is unborn, which is the only state with nothing to name.
    """
    head_sha = _head_commit_sha(library_path)
    if head_sha is None:
        return None

    branch = _current_branch(library_path)
    if branch is not None:
        return branch

    return _tag_at_head(library_path) or head_sha


def get_git_info(library_path: Path) -> tuple[str | None, str | None]:
    """Get both the git remote URL and current ref for a library.

    Prefer this over calling get_git_remote() + get_current_ref() separately when both
    values are needed: those functions each re-run is_git_repository(), and each raises
    where this one degrades to None.

    This runs for every library on every metadata load, where git details are informational
    and a library must still load without them. A missing git installation therefore reports
    "unavailable" here instead of raising the way the single-value accessors do.

    Returns:
        tuple[str | None, str | None]: (git_remote, git_ref), each None if unavailable.
    """
    if not is_git_repository(library_path):
        return None, None

    try:
        return _remote_url(library_path), _describe_head(library_path)
    except GitNotFoundError:
        logger.debug("Reporting no git details for %s: git is not installed", library_path)
        return None, None


def get_git_remote(library_path: Path) -> str | None:
    """Get the git remote URL for a library directory.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The remote URL if found, None if not a git repository or no remote configured.

    Raises:
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        return None

    return _remote_url(library_path)


def get_current_ref(library_path: Path) -> str | None:
    """Get the current git reference (branch, tag, or commit) for a library directory.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The current git reference (branch name, tag name, or commit SHA) if found, None if not a git repository.

    Raises:
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        logger.debug("Path %s is not a git repository", library_path)
        return None

    ref = _describe_head(library_path)
    if ref is None:
        logger.debug("Repository at %s has unborn HEAD (no commits)", library_path)
    return ref


def get_current_tag(library_path: Path) -> str | None:
    """Get the current tag name if HEAD is pointing to a tag.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The current tag name if found, None if not on a tag or not a git repository.

    Raises:
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        return None

    if _head_commit_sha(library_path) is None:
        return None

    return _tag_at_head(library_path)


def is_on_tag(library_path: Path) -> bool:
    """Check if HEAD is currently pointing to a tag.

    Args:
        library_path: The path to the library directory.

    Returns:
        bool: True if HEAD is on a tag, False otherwise.
    """
    return get_current_tag(library_path) is not None


def get_local_commit_sha(library_path: Path) -> str | None:
    """Get the current HEAD commit SHA for a library directory.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The full commit SHA if found, None if not a git repository or HEAD is unborn.

    Raises:
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        return None

    return _head_commit_sha(library_path)


def get_git_repository_root(library_path: Path) -> Path | None:
    """Get the root directory of the git repository containing the given path.

    Args:
        library_path: A path within a git repository.

    Returns:
        Path | None: The root directory of the git repository, or None if not in a git repository.
            A bare repository is not recognized, matching is_git_repository().

    Raises:
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        return None

    # --show-cdup is relative to library_path, so the root comes back in the caller's
    # own path vocabulary. --show-toplevel would resolve symlinks along the way.
    cdup = _try_git(["rev-parse", "--show-cdup"], library_path)
    if cdup is None:
        return None
    return Path(os.path.normpath(library_path / cdup))


def has_uncommitted_changes(library_path: Path) -> bool:
    """Check if a repository has uncommitted changes (including untracked files).

    Args:
        library_path: The path to the library directory.

    Returns:
        True if there are uncommitted changes or untracked files, False otherwise.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot check status: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    status = _run_git(
        ["status", "--porcelain"],
        error_msg=f"Failed to check git status at {library_path}",
        cwd=library_path,
        error_cls=GitRepositoryError,
    )
    return bool(status)


def _resolve_update_upstream(library_path: Path) -> str:
    """Validate that a branch-based update is possible and return the upstream ref name.

    Returns:
        str: The upstream of the current branch, e.g. "origin/main".

    Raises:
        GitRepositoryError: If validation fails.
        GitPullError: If repository state is invalid for update.
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot update: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    branch = _current_branch(library_path)
    if branch is None:
        msg = f"Repository at {library_path} has detached HEAD"
        raise GitPullError(msg)

    upstream = _upstream_ref(library_path)
    if upstream is None:
        msg = f"No upstream branch set for {branch} at {library_path}"
        raise GitPullError(msg)

    if _remote_url(library_path) is None:
        msg = f"No origin remote found for repository at {library_path}"
        raise GitPullError(msg)

    return upstream


def git_update_from_remote(library_path: Path, *, overwrite_existing: bool = False) -> None:
    """Update a library from remote by resetting to match upstream exactly.

    This function uses git fetch + git reset --hard to force the local repository
    to match the remote state. This is appropriate for library consumption where
    local modifications should not be preserved.

    Args:
        library_path: The path to the library directory.
        overwrite_existing: If True, discard any uncommitted local changes.
            If False, fail if uncommitted changes exist.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
        GitPullError: If the update operation fails or uncommitted changes exist
            when overwrite_existing=False.
        GitNotFoundError: If git is not installed.
    """
    upstream = _resolve_update_upstream(library_path)

    if has_uncommitted_changes(library_path):
        if not overwrite_existing:
            msg = f"Cannot update library at {library_path}: You have uncommitted changes. Use overwrite_existing=True to discard them."
            raise GitPullError(msg)

        logger.warning("Discarding uncommitted changes at %s", library_path)

    error_msg = f"Git error during update at {library_path}"
    _run_git(["fetch", "origin"], error_msg=error_msg, cwd=library_path, error_cls=GitPullError)
    _run_git(["reset", "--hard", upstream], error_msg=error_msg, cwd=library_path, error_cls=GitPullError)

    logger.debug("Successfully updated library at %s to match remote %s", library_path, upstream)


def update_to_moving_tag(library_path: Path, tag_name: str, *, overwrite_existing: bool = False) -> None:
    """Update library to the latest version of a moving tag.

    This function is designed for tags that are force-pushed to point to new commits
    (e.g., a 'latest' tag that always points to the newest release).

    Args:
        library_path: The path to the library directory.
        tag_name: The name of the tag to update to (e.g., "latest").
        overwrite_existing: If True, discard any uncommitted local changes.
            If False, fail if uncommitted changes exist.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
        GitPullError: If the tag update operation fails or uncommitted changes exist
            when overwrite_existing=False.
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot update tag: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    _reject_option_like(tag_name, "tag name", GitPullError)

    if _remote_url(library_path) is None:
        msg = f"No origin remote found for repository at {library_path}"
        raise GitPullError(msg)

    if has_uncommitted_changes(library_path):
        if not overwrite_existing:
            msg = f"Cannot update library at {library_path}: You have uncommitted changes. Use overwrite_existing=True to discard them."
            raise GitPullError(msg)

        logger.warning("Discarding uncommitted changes at %s", library_path)

    error_msg = f"Git error during tag update at {library_path}"

    # --force is what makes this work for a moving tag: without it git refuses to
    # replace a local tag whose remote counterpart now points at a new commit.
    _run_git(["fetch", "--tags", "--force", "origin"], error_msg=error_msg, cwd=library_path, error_cls=GitPullError)

    tag_ref = f"refs/tags/{tag_name}"
    if not _ref_exists(library_path, tag_ref):
        msg = f"Tag {tag_name} not found at {library_path}"
        raise GitPullError(msg)

    checkout = ["checkout", "--detach", tag_ref]
    if overwrite_existing:
        checkout.insert(1, "--force")
    _run_git(checkout, error_msg=error_msg, cwd=library_path, error_cls=GitPullError)

    logger.debug("Successfully updated library at %s to tag %s", library_path, tag_name)


def update_library_git(library_path: Path, *, overwrite_existing: bool = False) -> None:
    """Update a library to the latest version using the appropriate git strategy.

    This function automatically detects whether the library uses a branch-based or
    tag-based workflow and applies the correct update mechanism:
    - Branch-based: Uses git fetch + git reset --hard
    - Tag-based: Uses git fetch --tags --force + git checkout

    Args:
        library_path: The path to the library directory.
        overwrite_existing: If True, discard any uncommitted local changes.
            If False, fail if uncommitted changes exist.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
        GitPullError: If the update operation fails or uncommitted changes exist
            when overwrite_existing=False.
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot update: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    if _current_branch(library_path) is None:
        # Detached HEAD - likely on a tag
        tag_name = get_current_tag(library_path)
        if tag_name is None:
            msg = f"Repository at {library_path} is in detached HEAD state but not on a known tag. Cannot auto-update."
            raise GitPullError(msg)

        logger.debug("Detected tag-based workflow for %s (tag: %s)", library_path, tag_name)
        update_to_moving_tag(library_path, tag_name, overwrite_existing=overwrite_existing)
    else:
        logger.debug("Detected branch-based workflow for %s", library_path)
        git_update_from_remote(library_path, overwrite_existing=overwrite_existing)


def switch_branch(library_path: Path, branch_name: str) -> None:
    """Switch to a different branch in a library directory.

    Fetches from remote first, then checks out the specified branch.
    If the branch doesn't exist locally, creates a tracking branch from remote.

    Args:
        library_path: The path to the library directory.
        branch_name: The name of the branch to switch to.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
        GitRefError: If the branch switch operation fails.
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot switch branch: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    _reject_option_like(branch_name, "branch name", GitRefError)

    if _remote_url(library_path) is None:
        msg = f"No origin remote found for repository at {library_path}"
        raise GitRefError(msg)

    error_msg = f"Git error during branch switch at {library_path}"
    _run_git(["fetch", "origin"], error_msg=error_msg, cwd=library_path, error_cls=GitRefError)

    if _ref_exists(library_path, f"refs/heads/{branch_name}"):
        _run_git(["checkout", branch_name], error_msg=error_msg, cwd=library_path, error_cls=GitRefError)
        logger.debug("Checked out existing local branch %s at %s", branch_name, library_path)
        return

    remote_branch_name = f"origin/{branch_name}"
    if not _ref_exists(library_path, f"refs/remotes/{remote_branch_name}"):
        msg = f"Branch {branch_name} not found locally or on remote at {library_path}"
        raise GitRefError(msg)

    _run_git(
        ["checkout", "-b", branch_name, "--track", remote_branch_name],
        error_msg=error_msg,
        cwd=library_path,
        error_cls=GitRefError,
    )
    logger.debug(
        "Created and checked out tracking branch %s from %s at %s", branch_name, remote_branch_name, library_path
    )


def switch_branch_or_tag(library_path: Path, ref_name: str) -> None:
    """Switch to a different branch or tag in a library directory.

    Fetches from remote first, then checks out the specified branch or tag.
    Automatically detects whether the ref is a branch or tag.

    Args:
        library_path: The path to the library directory.
        ref_name: The name of the branch or tag to switch to.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
        GitRefError: If the switch operation fails.
        GitNotFoundError: If git is not installed.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot switch ref: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    _reject_option_like(ref_name, "ref name", GitRefError)

    error_msg = f"Git error during ref switch at {library_path}"
    # --tags fetches tags on top of the configured refspec, so one call updates
    # remote-tracking branches and force-updates moved tags.
    _run_git(["fetch", "--tags", "--force", "origin"], error_msg=error_msg, cwd=library_path, error_cls=GitRefError)

    remote_branch_name = f"origin/{ref_name}"
    if _ref_exists(library_path, f"refs/tags/{ref_name}"):
        _run_git(
            ["checkout", "--detach", f"refs/tags/{ref_name}"],
            error_msg=error_msg,
            cwd=library_path,
            error_cls=GitRefError,
        )
    elif _ref_exists(library_path, f"refs/remotes/{remote_branch_name}"):
        # -B resets an existing local branch onto the freshly fetched remote tip.
        _run_git(
            ["checkout", "-B", ref_name, "--track", remote_branch_name],
            error_msg=error_msg,
            cwd=library_path,
            error_cls=GitRefError,
        )
    elif _ref_exists(library_path, f"refs/heads/{ref_name}"):
        _run_git(["checkout", ref_name], error_msg=error_msg, cwd=library_path, error_cls=GitRefError)
    else:
        msg = f"Ref {ref_name} not found at {library_path}"
        raise GitRefError(msg)

    logger.debug("Checked out %s at %s", ref_name, library_path)


def clone_repository(git_url: str, target_path: Path, branch_tag_commit: str | None = None) -> None:
    """Clone a git repository to a target directory.

    Args:
        git_url: The git repository URL to clone (HTTPS or SSH).
        target_path: The target directory path to clone into. A relative path is anchored to the
            current working directory.
        branch_tag_commit: Optional branch, tag, or commit to checkout after cloning.

    Raises:
        GitCloneError: If cloning fails or target path already exists.
        GitNotFoundError: If git is not installed.
    """
    # The clone runs in a throwaway directory (see _run_git_detached), so git would resolve a
    # relative target against that directory and the clone would be discarded with it. Anchor to
    # the caller's working directory instead, before anything else reads the path. Deliberately
    # not canonicalize_for_io: it applies the Windows \\?\ long-path prefix, which git rejects.
    target_path = target_path.absolute()

    if target_path.exists():
        msg = f"Cannot clone: target path {target_path} already exists"
        raise GitCloneError(msg)

    _reject_option_like(git_url, "git URL", GitCloneError)
    if branch_tag_commit:
        _reject_option_like(branch_tag_commit, "ref", GitCloneError)

    _run_git_detached(
        ["clone", git_url, str(target_path)],
        error_msg=f"Git error while cloning {git_url} to {target_path}",
        error_cls=GitCloneError,
    )

    if branch_tag_commit:
        # A single checkout covers all three: a remote branch name becomes a local
        # tracking branch, a tag or commit lands on a detached HEAD.
        _run_git(
            ["checkout", branch_tag_commit],
            error_msg=f"Failed to checkout {branch_tag_commit} in {target_path}",
            cwd=target_path,
            error_cls=GitCloneError,
        )
        logger.debug("Checked out %s in %s", branch_tag_commit, target_path)


def _extract_library_version_from_json(json_path: Path, remote_url: str) -> str:
    """Extract library version from a griptape_nodes_library.json file.

    Args:
        json_path: Path to the library JSON file.
        remote_url: Git remote URL (for error messages).

    Returns:
        str: The library version string.

    Raises:
        GitCloneError: If JSON is invalid or version is missing.
    """
    import json

    try:
        with json_path.open(encoding="utf-8") as f:
            library_data = json.load(f)
    except json.JSONDecodeError as e:
        msg = f"JSON decode error reading library metadata from {remote_url}: {e}"
        raise GitCloneError(msg) from e

    if "metadata" not in library_data:
        msg = f"No metadata found in griptape_nodes_library.json from {remote_url}"
        raise GitCloneError(msg)

    if "library_version" not in library_data["metadata"]:
        msg = f"No library_version found in metadata from {remote_url}"
        raise GitCloneError(msg)

    return library_data["metadata"]["library_version"]


def sparse_checkout_library_json(remote_url: str, ref: str = "HEAD") -> LibraryJsonCheckout:
    """Fetch a library's JSON metadata from a git remote without a full clone.

    Uses a sparse checkout so only files matching the library JSON patterns are
    downloaded, rather than the whole repository.

    Args:
        remote_url: The git repository URL (HTTPS or SSH).
        ref: The git reference (branch, tag, or commit) to checkout. Defaults to HEAD.

    Returns:
        LibraryJsonCheckout: The library version, commit SHA, commit datetime, and library data.

    Raises:
        GitCloneError: If the checkout fails or library metadata is invalid.
        GitNotFoundError: If git is not installed.
    """
    _reject_option_like(remote_url, "git URL", GitCloneError)
    _reject_option_like(ref, "ref", GitCloneError)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        def run(args: list[str], error_msg: str) -> str:
            return _run_git(args, error_msg=error_msg, cwd=temp_path, error_cls=GitCloneError)

        run(["init"], "Git init failed")
        run(["remote", "add", "origin", remote_url], "Git remote add failed")
        run(["config", "core.sparseCheckout", "true"], "Git sparse checkout config failed")

        # Configure sparse-checkout patterns
        sparse_checkout_file = temp_path / ".git" / "info" / "sparse-checkout"
        sparse_checkout_file.parent.mkdir(parents=True, exist_ok=True)
        patterns = [
            "griptape_nodes_library.json",
            "*/griptape_nodes_library.json",
            "*/*/griptape_nodes_library.json",
            "griptape-nodes-library.json",
            "*/griptape-nodes-library.json",
            "*/*/griptape-nodes-library.json",
        ]
        sparse_checkout_file.write_text("\n".join(patterns), encoding="utf-8")

        run(["fetch", "--depth=1", "origin", ref], f"Git fetch failed for {ref}")
        run(["checkout", "FETCH_HEAD"], "Git checkout failed")

        library_json_path = find_file_in_directory(temp_path, "griptape[-_]nodes[-_]library.json")
        if library_json_path is None:
            msg = f"No library JSON file found in sparse checkout from {remote_url}"
            raise GitCloneError(msg)

        library_version = _extract_library_version_from_json(library_json_path, remote_url)
        commit_sha = run(["rev-parse", "HEAD"], "Git rev-parse failed")

        # Committer date of the checked-out commit (strict ISO 8601). This is a best-effort
        # field for the update age gate; a failure here must not fail the whole checkout,
        # which backs the core version-check path, so degrade to None on error.
        commit_datetime = parse_commit_datetime(_try_git(["log", "-1", "--format=%cI", "HEAD"], temp_path) or "")

        # Read the JSON data before temp directory is deleted
        try:
            with library_json_path.open(encoding="utf-8") as f:
                library_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            msg = f"Failed to read library file from {remote_url}: {e}"
            raise GitCloneError(msg) from e

        return LibraryJsonCheckout(
            library_version=library_version,
            commit_sha=commit_sha,
            commit_datetime=commit_datetime,
            library_data=library_data,
        )


def remote_ref_exists(remote_url: str, ref: str) -> bool:
    """Check whether a branch or tag named ``ref`` exists on a git remote.

    Commit SHAs are not advertised as named refs, so a detached HEAD pointing at a
    bare commit reports False.

    Args:
        remote_url: The git repository URL (HTTPS or SSH).
        ref: The branch or tag name to look for on the remote.

    Returns:
        bool: True if a matching branch or tag exists on the remote, False otherwise.

    Raises:
        GitRemoteError: If the remote cannot be queried.
        GitNotFoundError: If git is not installed.
    """
    _reject_option_like(remote_url, "git URL", GitRemoteError)
    _reject_option_like(ref, "ref", GitRemoteError)

    refs = _run_git_detached(
        ["ls-remote", "--heads", "--tags", remote_url, ref],
        error_msg=f"Failed to query remote refs from {remote_url}",
        error_cls=GitRemoteError,
    )
    return bool(refs)
