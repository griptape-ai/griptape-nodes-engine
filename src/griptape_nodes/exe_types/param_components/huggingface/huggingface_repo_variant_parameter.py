"""HuggingFace parameter class that supports repo + variant/subfolder selection."""

import logging
from pathlib import Path

from huggingface_hub.constants import HF_HUB_CACHE

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.huggingface.huggingface_model_parameter import HuggingFaceModelParameter

logger = logging.getLogger("griptape_nodes")


def _get_repo_cache_path(repo_id: str) -> Path | None:
    """Get the cache path for a repo if it exists."""
    cache_path = Path(HF_HUB_CACHE)
    if not cache_path.exists():
        return None

    # Convert repo_id to cache folder format: owner/repo -> models--owner--repo
    folder_name = f"models--{repo_id.replace('/', '--')}"
    repo_path = cache_path / folder_name

    if repo_path.exists():
        return repo_path
    return None


def _get_snapshot_path(repo_path: Path) -> Path | None:
    """Get the latest snapshot path for a repo."""
    snapshots_dir = repo_path / "snapshots"
    if not snapshots_dir.exists():
        return None

    snapshots = [p for p in snapshots_dir.iterdir() if p.is_dir()]
    if not snapshots:
        return None

    # Return the most recent snapshot
    return max(snapshots, key=lambda p: p.stat().st_mtime)


def _list_variants_in_cache(repo_id: str, variants: list[str]) -> list[tuple[str, str, str]]:
    """List (repo_id, variant, revision) tuples for variants that exist in cache.

    Args:
        repo_id: The HuggingFace repo ID (e.g., "Lightricks/LTX-2")
        variants: List of variant/subfolder names to check for (e.g., ["ltx-2-19b-dev", "ltx-2-19b-dev-fp8"])

    Returns:
        List of (repo_id, variant, revision) tuples for variants found in cache
    """
    repo_path = _get_repo_cache_path(repo_id)
    if repo_path is None:
        return []

    snapshot_path = _get_snapshot_path(repo_path)
    if snapshot_path is None:
        return []

    revision = snapshot_path.name
    results = []

    for variant in variants:
        # Check for variant as a directory (subfolder model structure)
        variant_dir_path = snapshot_path / variant
        if variant_dir_path.exists() and variant_dir_path.is_dir():
            results.append((repo_id, variant, revision))
            continue

        # Check for variant as a .safetensors file (single-file model structure)
        variant_file_path = snapshot_path / f"{variant}.safetensors"
        if variant_file_path.exists() and variant_file_path.is_file():
            results.append((repo_id, variant, revision))

    return results


class HuggingFaceRepoVariantParameter(HuggingFaceModelParameter):
    """Parameter class for selecting a variant/subfolder within a HuggingFace repo.

    Use this when a single repo contains multiple model variants as subfolders.
    For example, Lightricks/LTX-2 contains:
    - ltx-2-19b-dev
    - ltx-2-19b-dev-fp8
    - ltx-2-19b-dev-fp4
    """

    def __init__(
        self,
        node: BaseNode,
        repo_id: str,
        variants: list[str],
        parameter_name: str = "model",
    ):
        """Initialize the parameter.

        Args:
            node: The node this parameter belongs to
            repo_id: The HuggingFace repo ID (e.g., "Lightricks/LTX-2")
            variants: List of variant/subfolder names (e.g., ["ltx-2-19b-dev", "ltx-2-19b-dev-fp8"])
            parameter_name: Name of the parameter (default: "model")
        """
        super().__init__(node, parameter_name)
        self._repo_id = repo_id
        self._variants = variants
        self.refresh_parameters()

    @classmethod
    def _repo_variant_to_key(cls, repo_id: str, variant: str) -> str:
        """Convert repo_id and variant to a display key."""
        return f"{repo_id}/{variant}"

    @classmethod
    def _key_to_repo_variant(cls, key: str) -> tuple[str, str]:
        """Parse a display key back to repo_id and variant.

        Key format: "owner/repo/variant"
        """
        parts = key.rsplit("/", 1)
        if len(parts) == 2:  # noqa: PLR2004
            return parts[0], parts[1]
        # Fallback: treat entire key as repo_id with empty variant
        return key, ""

    def fetch_repo_revisions(self) -> list[tuple[str, str]]:
        """Fetch available variants from cache.

        Returns list of (display_key, revision) tuples where display_key is "repo_id/variant".
        """
        variant_revisions = _list_variants_in_cache(self._repo_id, self._variants)
        return [
            (self._repo_variant_to_key(repo_id, variant), revision) for repo_id, variant, revision in variant_revisions
        ]

    def get_download_commands(self) -> list[str]:
        """Return download commands for the repo."""
        return [f'huggingface-cli download "{self._repo_id}"']

    def get_download_models(self) -> list[str]:
        """Return list of model names for download."""
        return [self._repo_id]

    def get_not_downloaded_choices(self) -> list[str]:
        downloaded_keys = {key for key, _ in self.list_repo_revisions()}
        return [
            self._repo_variant_to_key(self._repo_id, v)
            for v in self._variants
            if self._repo_variant_to_key(self._repo_id, v) not in downloaded_keys
        ]

    def _get_model_search_term(self, choice: str) -> str:
        repo_id, _ = self._key_to_repo_variant(choice)
        return repo_id

    def get_repo_variant_revision(self) -> tuple[str, str, str]:
        """Get the selected repo_id, variant, and revision.

        Returns:
            Tuple of (repo_id, variant, revision)
        """
        repo_key, revision = self.get_repo_revision()
        repo_id, variant = self._key_to_repo_variant(repo_key)
        return repo_id, variant, revision
