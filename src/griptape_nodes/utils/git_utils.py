"""Git utilities for library updates."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import pygit2

from griptape_nodes.utils.file_utils import find_file_in_directory

# Common SSH key paths to try when SSH agent doesn't have keys loaded
_SSH_KEY_PATHS = [
    Path.home() / ".ssh" / "id_ed25519",
    Path.home() / ".ssh" / "id_rsa",
    Path.home() / ".ssh" / "id_ecdsa",
]

logger = logging.getLogger("griptape_nodes")


class GitError(Exception):
    """Base exception for git operations."""


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


def get_git_remote(library_path: Path) -> str | None:
    """Get the git remote URL for a library directory.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The remote URL if found, None if not a git repository or no remote configured.

    Raises:
        GitRemoteError: If an error occurs while accessing the git remote.
    """
    if not is_git_repository(library_path):
        return None

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            return None

        repo = pygit2.Repository(repo_path)

        # Access remote by indexing (raises KeyError if not found)
        try:
            remote = repo.remotes["origin"]
        except (KeyError, IndexError):
            return None
        else:
            return remote.url

    except pygit2.GitError as e:
        msg = f"Error getting git remote for {library_path}: {e}"
        raise GitRemoteError(msg) from e


def get_current_ref(library_path: Path) -> str | None:
    """Get the current git reference (branch, tag, or commit) for a library directory.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The current git reference (branch name, tag name, or commit SHA) if found, None if not a git repository.

    Raises:
        GitRefError: If an error occurs while getting the current git reference.
    """
    if not is_git_repository(library_path):
        logger.debug("Path %s is not a git repository", library_path)
        return None

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            logger.debug("Could not discover git repository at %s", library_path)
            return None

        repo = pygit2.Repository(repo_path)

        # Check if HEAD is unborn (no commits yet)
        if repo.head_is_unborn:
            logger.debug("Repository at %s has unborn HEAD (no commits)", library_path)
            return None

        # Check if HEAD is detached
        if repo.head_is_detached:
            # HEAD is detached - check if it's pointing to a tag
            tag_name = get_current_tag(library_path)
            if tag_name:
                logger.debug("Repository at %s has detached HEAD on tag %s", library_path, tag_name)
                return tag_name

            # No tag found, return the commit SHA as fallback
            head_commit = repo.head.target
            logger.debug("Repository at %s has detached HEAD at commit %s", library_path, head_commit)
            return str(head_commit)

    except pygit2.GitError as e:
        msg = f"Error getting current git reference for {library_path}: {e}"
        raise GitRefError(msg) from e
    else:
        # Get the current git reference name (branch)
        return repo.head.shorthand


def get_current_tag(library_path: Path) -> str | None:
    """Get the current tag name if HEAD is pointing to a tag.

    Args:
        library_path: The path to the library directory.

    Returns:
        str | None: The current tag name if found, None if not on a tag or not a git repository.

    Raises:
        GitError: If an error occurs while getting the current tag.
    """
    if not is_git_repository(library_path):
        return None

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            return None

        repo = pygit2.Repository(repo_path)

        # Get the current HEAD commit
        if repo.head_is_unborn:
            return None

        head_commit = repo.head.target

        # Check all tags to see if any point to HEAD
        for tag_name in repo.references:
            if not tag_name.startswith("refs/tags/"):
                continue

            tag_ref = repo.references[tag_name]
            # Handle both lightweight and annotated tags
            if hasattr(tag_ref, "peel"):
                tag_target = tag_ref.peel(pygit2.Commit).id
            else:
                tag_target = tag_ref.target

            if tag_target == head_commit:
                # Return tag name without refs/tags/ prefix
                return tag_name.replace("refs/tags/", "")
    except pygit2.GitError as e:
        msg = f"Error getting current tag for {library_path}: {e}"
        raise GitError(msg) from e
    else:
        return None


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
        str | None: The full commit SHA if found, None if not a git repository or error occurs.

    Raises:
        GitError: If an error occurs while getting the commit SHA.
    """
    if not is_git_repository(library_path):
        return None

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            return None

        repo = pygit2.Repository(repo_path)

        if repo.head_is_unborn:
            return None

        return str(repo.head.target)

    except pygit2.GitError as e:
        msg = f"Error getting commit SHA for {library_path}: {e}"
        raise GitError(msg) from e


def get_git_repository_root(library_path: Path) -> Path | None:
    """Get the root directory of the git repository containing the given path.

    Args:
        library_path: A path within a git repository.

    Returns:
        Path | None: The root directory of the git repository, or None if not in a git repository.

    Raises:
        GitRepositoryError: If an error occurs while accessing the git repository.
    """
    if not is_git_repository(library_path):
        return None

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            return None

        # discover_repository returns path to .git directory
        # For a normal repo: /path/to/repo/.git
        # For a bare repo: /path/to/repo.git
        git_dir = Path(repo_path)

        # Check if it's a bare repository
        if git_dir.name.endswith(".git") and git_dir.is_dir():
            repo = pygit2.Repository(repo_path)
            if repo.is_bare:
                return git_dir

    except pygit2.GitError as e:
        msg = f"Error getting git repository root for {library_path}: {e}"
        raise GitRepositoryError(msg) from e
    else:
        # Normal repository - return parent of .git directory
        return git_dir.parent


def has_uncommitted_changes(library_path: Path) -> bool:
    """Check if a repository has uncommitted changes (including untracked files).

    Args:
        library_path: The path to the library directory.

    Returns:
        True if there are uncommitted changes or untracked files, False otherwise.

    Raises:
        GitRepositoryError: If the path is not a valid git repository.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot check status: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot check status: {library_path} is not a git repository"
            raise GitRepositoryError(msg)

        repo = pygit2.Repository(repo_path)
        status = repo.status()
        return len(status) > 0

    except pygit2.GitError as e:
        msg = f"Failed to check git status at {library_path}: {e}"
        raise GitRepositoryError(msg) from e


def _validate_branch_update_preconditions(library_path: Path) -> None:
    """Validate preconditions for branch-based update.

    Raises:
        GitRepositoryError: If validation fails.
        GitPullError: If repository state is invalid for update.
    """
    if not is_git_repository(library_path):
        msg = f"Cannot update: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot discover repository at {library_path}"
            raise GitRepositoryError(msg)

        repo = pygit2.Repository(repo_path)

        if repo.head_is_detached:
            msg = f"Repository at {library_path} has detached HEAD"
            raise GitPullError(msg)

        current_branch = repo.branches.get(repo.head.shorthand)
        if current_branch is None:
            msg = f"Cannot get current branch for repository at {library_path}"
            raise GitPullError(msg)

        if current_branch.upstream is None:
            msg = f"No upstream branch set for {current_branch.branch_name} at {library_path}"
            raise GitPullError(msg)

        try:
            _ = repo.remotes["origin"]
        except (KeyError, IndexError) as e:
            msg = f"No origin remote found for repository at {library_path}"
            raise GitPullError(msg) from e

    except pygit2.GitError as e:
        msg = f"Git error during update at {library_path}: {e}"
        raise GitPullError(msg) from e


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
    """
    _validate_branch_update_preconditions(library_path)

    if has_uncommitted_changes(library_path):
        if not overwrite_existing:
            msg = f"Cannot update library at {library_path}: You have uncommitted changes. Use overwrite_existing=True to discard them."
            raise GitPullError(msg)

        logger.warning("Discarding uncommitted changes at %s", library_path)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot update: {library_path} is not a git repository"
            raise GitPullError(msg)

        repo = pygit2.Repository(repo_path)

        # Get remote and fetch
        remote = repo.remotes["origin"]
        remote.fetch()

        # Get upstream branch reference
        try:
            upstream_name = repo.branches.get(repo.head.shorthand).upstream.branch_name
            upstream_ref = repo.references.get(f"refs/remotes/{upstream_name}")
            if upstream_ref is None:
                msg = f"Failed to find upstream reference {upstream_name} at {library_path}"
                raise GitPullError(msg)
            upstream_oid = upstream_ref.target
        except (pygit2.GitError, AttributeError) as e:
            msg = f"Failed to determine upstream branch at {library_path}: {e}"
            raise GitPullError(msg) from e

        # Hard reset to upstream
        repo.reset(upstream_oid, pygit2.enums.ResetMode.HARD)

        logger.debug("Successfully updated library at %s to match remote %s", library_path, upstream_name)

    except pygit2.GitError as e:
        msg = f"Git error during update at {library_path}: {e}"
        raise GitPullError(msg) from e


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
    """
    if not is_git_repository(library_path):
        msg = f"Cannot update tag: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot discover repository at {library_path}"
            raise GitRepositoryError(msg)

        repo = pygit2.Repository(repo_path)

        # Check for origin remote
        try:
            _ = repo.remotes["origin"]
        except (KeyError, IndexError) as e:
            msg = f"No origin remote found for repository at {library_path}"
            raise GitPullError(msg) from e

    except pygit2.GitError as e:
        msg = f"Git error during tag update at {library_path}: {e}"
        raise GitPullError(msg) from e

    # Check for uncommitted changes
    if has_uncommitted_changes(library_path):
        if not overwrite_existing:
            msg = f"Cannot update library at {library_path}: You have uncommitted changes. Use overwrite_existing=True to discard them."
            raise GitPullError(msg)

        logger.warning("Discarding uncommitted changes at %s", library_path)

    # Use pygit2 to fetch tags and checkout
    try:
        # Step 1: Delete local tag to allow fetch to update it (pygit2 doesn't honor +force)
        tag_ref = f"refs/tags/{tag_name}"
        if tag_ref in repo.references:
            repo.references.delete(tag_ref)
            logger.debug("Deleted local tag %s to allow force-update", tag_name)

        # Step 2: Fetch all tags (will create the deleted tag with new commit)
        remote = repo.remotes["origin"]
        remote.fetch(refspecs=["+refs/tags/*:refs/tags/*"])

        # Step 3: Checkout the tag with force to discard local changes
        if tag_ref not in repo.references:
            msg = f"Tag {tag_name} not found at {library_path}"
            raise GitPullError(msg)

        strategy = pygit2.enums.CheckoutStrategy.FORCE if overwrite_existing else pygit2.enums.CheckoutStrategy.SAFE
        repo.checkout(tag_ref, strategy=strategy)

        logger.debug("Successfully updated library at %s to tag %s", library_path, tag_name)

    except pygit2.GitError as e:
        msg = f"Git error during tag update at {library_path}: {e}"
        raise GitPullError(msg) from e


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
    """
    if not is_git_repository(library_path):
        msg = f"Cannot update: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot discover repository at {library_path}"
            raise GitRepositoryError(msg)

        repo = pygit2.Repository(repo_path)

        # Detect workflow type
        if repo.head_is_detached:
            # Detached HEAD - likely on a tag
            tag_name = get_current_tag(library_path)
            if tag_name is None:
                msg = f"Repository at {library_path} is in detached HEAD state but not on a known tag. Cannot auto-update."
                raise GitPullError(msg)

            logger.debug("Detected tag-based workflow for %s (tag: %s)", library_path, tag_name)
            update_to_moving_tag(library_path, tag_name, overwrite_existing=overwrite_existing)
        else:
            # On a branch - use fetch + reset to match remote
            logger.debug("Detected branch-based workflow for %s", library_path)
            git_update_from_remote(library_path, overwrite_existing=overwrite_existing)

    except pygit2.GitError as e:
        msg = f"Git error during library update at {library_path}: {e}"
        raise GitPullError(msg) from e


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
    """
    if not is_git_repository(library_path):
        msg = f"Cannot switch branch: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot discover repository at {library_path}"
            raise GitRepositoryError(msg)

        repo = pygit2.Repository(repo_path)

        # Get origin remote
        try:
            remote = repo.remotes["origin"]
        except (KeyError, IndexError) as e:
            msg = f"No origin remote found for repository at {library_path}"
            raise GitRefError(msg) from e

        # Fetch from remote first
        remote.fetch()

        # Try to find the branch locally first
        local_branch = repo.branches.get(branch_name)

        if local_branch is not None:
            # Branch exists locally, just check it out
            repo.checkout(local_branch)
            logger.debug("Checked out existing local branch %s at %s", branch_name, library_path)
            return

        # Branch doesn't exist locally, try to find it on remote
        remote_branch_name = f"origin/{branch_name}"
        remote_branch = repo.branches.get(remote_branch_name)

        if remote_branch is None:
            msg = f"Branch {branch_name} not found locally or on remote at {library_path}"
            raise GitRefError(msg)

        # Create local tracking branch from remote
        commit = repo.get(remote_branch.target)
        if commit is None:
            msg = f"Failed to get commit for remote branch {remote_branch_name} at {library_path}"
            raise GitRefError(msg)

        new_branch = repo.branches.local.create(branch_name, commit)  # type: ignore[arg-type]
        new_branch.upstream = remote_branch

        # Checkout the new branch
        repo.checkout(new_branch)
        logger.debug(
            "Created and checked out tracking branch %s from %s at %s", branch_name, remote_branch_name, library_path
        )

    except pygit2.GitError as e:
        msg = f"Git error during branch switch at {library_path}: {e}"
        raise GitRefError(msg) from e


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
    """
    if not is_git_repository(library_path):
        msg = f"Cannot switch ref: {library_path} is not a git repository"
        raise GitRepositoryError(msg)

    try:
        repo_path = pygit2.discover_repository(str(library_path))
        if repo_path is None:
            msg = f"Cannot switch ref: {library_path} is not a git repository"
            raise GitRefError(msg)

        repo = pygit2.Repository(repo_path)

        # Fetch both branches and tags from remote
        remote = repo.remotes["origin"]
        remote.fetch(refspecs=["+refs/tags/*:refs/tags/*"])
        remote.fetch()

        # Try to checkout the ref (works for both branches and tags)
        # First check if it's a tag
        tag_ref = f"refs/tags/{ref_name}"
        branch_ref = f"refs/remotes/origin/{ref_name}"

        if tag_ref in repo.references:
            repo.checkout(tag_ref)
        elif branch_ref in repo.references:
            # For remote branches, create/update local tracking branch and checkout
            remote_branch_name = f"origin/{ref_name}"
            remote_branch = repo.branches.get(remote_branch_name)

            if remote_branch is None:
                msg = f"Remote branch {remote_branch_name} not found at {library_path}"
                raise GitRefError(msg)

            commit = repo.get(remote_branch.target)
            if commit is None:
                msg = f"Failed to get commit for remote branch {remote_branch_name} at {library_path}"
                raise GitRefError(msg)

            # Create or update local branch
            if ref_name in repo.branches.local:
                local_branch = repo.branches.local[ref_name]
                local_branch.set_target(commit.id)
            else:
                local_branch = repo.branches.local.create(ref_name, commit)  # type: ignore[arg-type]

            local_branch.upstream = remote_branch
            repo.checkout(local_branch)
        elif ref_name in repo.branches:
            # Local branch
            repo.checkout(repo.branches[ref_name])
        else:
            msg = f"Ref {ref_name} not found at {library_path}"
            raise GitRefError(msg)

        logger.debug("Checked out %s at %s", ref_name, library_path)

    except pygit2.GitError as e:
        msg = f"Git error during ref switch at {library_path}: {e}"
        raise GitRefError(msg) from e


class _CredentialCallbacks(pygit2.RemoteCallbacks):
    """RemoteCallbacks that handle both SSH and public HTTPS authentication.

    When libgit2 requests SSH credentials (either for SSH URLs or HTTPS URLs
    rewritten via url.insteadOf git config), tries SSH key files then falls
    back to the SSH agent. For HTTPS, returns empty credentials (sufficient
    for public repos).
    """

    def credentials(self, url: str, username_from_url: str | None, allowed_types: int) -> object:  # type: ignore[override]
        ssh_types = (
            pygit2.enums.CredentialType.SSH_KEY
            | pygit2.enums.CredentialType.SSH_CUSTOM
            | pygit2.enums.CredentialType.SSH_MEMORY
        )
        if allowed_types & ssh_types:
            # libgit2 rewrote the HTTPS URL to SSH via a url.insteadOf git config rule,
            # or this is an SSH URL directly. Try key files first, then agent.
            for key_path in _SSH_KEY_PATHS:
                pub_key_path = key_path.with_suffix(key_path.suffix + ".pub")
                if key_path.exists() and pub_key_path.exists():
                    logger.debug("Using SSH key from %s for %s", key_path, url)
                    return pygit2.Keypair(username_from_url or "git", str(pub_key_path), str(key_path), "")
            logger.debug("No SSH key files found, falling back to SSH agent for %s", url)
            return pygit2.KeypairFromAgent(username_from_url or "git")

        return pygit2.UserPass("", "")


def _is_git_available() -> bool:
    """Check if git CLI is available on PATH.

    Returns:
        bool: True if git CLI is available, False otherwise.
    """
    try:
        subprocess.run(
            ["git", "--version"],  # noqa: S607
            capture_output=True,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    else:
        return True


def _run_git_command(args: list[str], cwd: str, error_msg: str) -> subprocess.CompletedProcess[str]:
    """Run a git command and raise GitCloneError on failure.

    Args:
        args: Git command arguments (e.g., ["git", "init"]).
        cwd: Working directory for the command.
        error_msg: Error message prefix to use if command fails.

    Returns:
        subprocess.CompletedProcess: The result of the command.

    Raises:
        GitCloneError: If the command returns a non-zero exit code.
    """
    result = subprocess.run(  # noqa: S603
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"{error_msg}: {result.stderr}"
        raise GitCloneError(msg)

    return result


def _checkout_branch_tag_or_commit(repo: pygit2.Repository, ref: str) -> None:
    """Check out a branch, tag, or commit in a repository.

    For branches, creates a local tracking branch from the remote.
    For tags and commits, checks out in detached HEAD state.

    Args:
        repo: The pygit2 Repository object.
        ref: The branch, tag, or commit reference to checkout.

    Raises:
        GitCloneError: If checkout fails.
    """
    # Try to resolve as a local branch first
    try:
        branch = repo.branches[ref]
        repo.checkout(branch)
    except (pygit2.GitError, KeyError, IndexError):
        pass
    else:
        logger.debug("Checked out local branch %s", ref)
        return

    # Try to resolve as a remote branch and create local tracking branch
    remote_ref = f"refs/remotes/origin/{ref}"
    remote_branch_exists = remote_ref in repo.references

    if remote_branch_exists:
        remote_branch_name = f"origin/{ref}"
        remote_branch = repo.branches.get(remote_branch_name)

        if remote_branch is not None:
            commit = repo.get(remote_branch.target)
            if commit is None:
                msg = f"Failed to get commit for remote branch {remote_branch_name}"
                raise GitCloneError(msg)

            # Create local tracking branch
            local_branch = repo.branches.local.create(ref, commit)  # type: ignore[arg-type]
            local_branch.upstream = remote_branch
            repo.checkout(local_branch)
            logger.debug("Checked out remote branch %s as local tracking branch", ref)
            return

    # Not a local or remote branch, try as tag or commit
    try:
        commit_obj = repo.revparse_single(ref)
        repo.checkout_tree(commit_obj)
        repo.set_head(commit_obj.id)
        logger.debug("Checked out %s as tag or commit", ref)
    except pygit2.GitError as e:
        msg = f"Failed to checkout {ref}: {e}"
        raise GitCloneError(msg) from e


def clone_repository(git_url: str, target_path: Path, branch_tag_commit: str | None = None) -> None:
    """Clone a git repository to a target directory.

    Args:
        git_url: The git repository URL to clone (HTTPS or SSH).
        target_path: The target directory path to clone into.
        branch_tag_commit: Optional branch, tag, or commit to checkout after cloning.

    Raises:
        GitCloneError: If cloning fails or target path already exists.
    """
    if target_path.exists():
        msg = f"Cannot clone: target path {target_path} already exists"
        raise GitCloneError(msg)

    try:
        # Clone the repository
        repo = pygit2.clone_repository(git_url, str(target_path), callbacks=_CredentialCallbacks())
        if repo is None:
            msg = f"Failed to clone repository from {git_url}"
            raise GitCloneError(msg)

        # Checkout specific branch/tag/commit if provided
        if branch_tag_commit:
            _checkout_branch_tag_or_commit(repo, branch_tag_commit)

    except pygit2.GitError as e:
        msg = f"Git error while cloning {git_url} to {target_path}: {e}"
        raise GitCloneError(msg) from e


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


def _sparse_checkout_with_git_cli(remote_url: str, ref: str) -> tuple[str, str, dict]:
    """Perform sparse checkout using git CLI to fetch only library JSON file.

    This is the most efficient method as it only downloads files matching the sparse
    checkout patterns, not the entire repository.

    Args:
        remote_url: The git repository URL (HTTPS or SSH).
        ref: The git reference (branch, tag, or commit) to checkout.

    Returns:
        tuple[str, str, dict]: A tuple of (library_version, commit_sha, library_data).

    Raises:
        GitCloneError: If sparse checkout fails or library metadata is invalid.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        try:
            # Initialize empty git repository
            _run_git_command(["git", "init"], temp_dir, "Git init failed")

            # Add remote
            _run_git_command(
                ["git", "remote", "add", "origin", remote_url],
                temp_dir,
                "Git remote add failed",
            )

            # Enable sparse checkout
            _run_git_command(
                ["git", "config", "core.sparseCheckout", "true"],
                temp_dir,
                "Git sparse checkout config failed",
            )

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

            # Fetch with depth 1 (shallow clone)
            _run_git_command(
                ["git", "fetch", "--depth=1", "origin", ref],
                temp_dir,
                f"Git fetch failed for {ref}",
            )

            # Checkout the ref
            _run_git_command(["git", "checkout", "FETCH_HEAD"], temp_dir, "Git checkout failed")

            # Find the library JSON file
            library_json_path = find_file_in_directory(temp_path, "griptape[-_]nodes[-_]library.json")
            if library_json_path is None:
                msg = f"No library JSON file found in sparse checkout from {remote_url}"
                raise GitCloneError(msg)

            # Extract version from JSON
            library_version = _extract_library_version_from_json(library_json_path, remote_url)

            # Get commit SHA
            rev_parse_result = _run_git_command(
                ["git", "rev-parse", "HEAD"],
                temp_dir,
                "Git rev-parse failed",
            )
            commit_sha = rev_parse_result.stdout.strip()

            # Read the JSON data before temp directory is deleted
            try:
                with library_json_path.open(encoding="utf-8") as f:
                    library_data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                msg = f"Failed to read library file from {remote_url}: {e}"
                raise GitCloneError(msg) from e

        except subprocess.SubprocessError as e:
            msg = f"Subprocess error during sparse checkout from {remote_url}: {e}"
            raise GitCloneError(msg) from e

        return (library_version, commit_sha, library_data)


def _shallow_clone_with_pygit2(remote_url: str, ref: str) -> tuple[str, str, dict]:
    """Perform shallow clone using pygit2 to fetch library JSON file.

    This is a fallback method when git CLI is not available. It downloads all files
    but with limited history (depth=1).

    Args:
        remote_url: The git repository URL (HTTPS or SSH).
        ref: The git reference (branch, tag, or commit) to checkout.

    Returns:
        tuple[str, str, dict]: A tuple of (library_version, commit_sha, library_data).

    Raises:
        GitCloneError: If clone fails or library metadata is invalid.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        repo = None  # Initialize for finally block

        try:
            callbacks = _CredentialCallbacks()

            # Shallow clone with depth=1
            # Note: We don't use checkout_branch here because it only works with branches,
            # not tags or commit SHAs. Instead, we'll fetch and checkout the ref manually.
            repo = pygit2.clone_repository(
                remote_url,
                str(temp_path),
                callbacks=callbacks,
                depth=1,
            )

            if repo is None:
                msg = f"Failed to clone repository from {remote_url}"
                raise GitCloneError(msg)

            # If a specific ref was requested (not HEAD), fetch and checkout that ref
            if ref != "HEAD":
                try:
                    # Fetch the specific ref (works for branches, tags, and commits)
                    remote = repo.remotes["origin"]
                    remote.fetch([ref], callbacks=callbacks, depth=1)

                    # Now resolve and checkout the ref
                    resolved_ref = repo.revparse_single(ref)
                    repo.checkout_tree(resolved_ref)
                    repo.set_head(resolved_ref.id)
                except (KeyError, pygit2.GitError) as e:
                    msg = f"Failed to fetch and checkout ref '{ref}' in clone from {remote_url}: {e}"
                    raise GitCloneError(msg) from e

            # Find the library JSON file
            library_json_path = find_file_in_directory(temp_path, "griptape[-_]nodes[-_]library.json")
            if library_json_path is None:
                msg = f"No library JSON file found in clone from {remote_url}"
                raise GitCloneError(msg)

            # Extract version from JSON
            library_version = _extract_library_version_from_json(library_json_path, remote_url)

            # Get commit SHA
            commit_sha = str(repo.head.target)

            # Read the JSON data before temp directory is deleted
            try:
                with library_json_path.open(encoding="utf-8") as f:
                    library_data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                msg = f"Failed to read library file from {remote_url}: {e}"
                raise GitCloneError(msg) from e

        except pygit2.GitError as e:
            msg = f"Git error during clone from {remote_url}: {e}"
            raise GitCloneError(msg) from e
        finally:
            # Release repository file handles before temp directory cleanup
            # Critical on Windows where open handles prevent directory deletion
            if repo is not None:
                repo.free()

        return (library_version, commit_sha, library_data)


def sparse_checkout_library_json(remote_url: str, ref: str = "HEAD") -> tuple[str, str, dict]:
    """Fetch library JSON file from a git repository.

    This function uses the most efficient method available:
    - If git CLI is available: uses sparse checkout (only downloads needed files)
    - Otherwise: falls back to pygit2 shallow clone (downloads all files with depth=1)

    Args:
        remote_url: The git repository URL (HTTPS or SSH).
        ref: The git reference (branch, tag, or commit) to checkout. Defaults to HEAD.

    Returns:
        tuple[str, str, dict]: A tuple of (library_version, commit_sha, library_data).

    Raises:
        GitCloneError: If the operation fails or library metadata is invalid.
    """
    if _is_git_available():
        logger.debug("Using git CLI for sparse checkout from %s", remote_url)
        return _sparse_checkout_with_git_cli(remote_url, ref)

    logger.debug("Git CLI not available, using pygit2 shallow clone from %s", remote_url)
    return _shallow_clone_with_pygit2(remote_url, ref)
