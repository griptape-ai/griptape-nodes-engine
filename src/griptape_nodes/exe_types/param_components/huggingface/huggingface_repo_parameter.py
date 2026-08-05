import logging

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.huggingface.huggingface_model_parameter import HuggingFaceModelParameter
from griptape_nodes.exe_types.param_components.huggingface.huggingface_utils import (
    list_all_repo_revisions_in_cache,
    list_repo_revisions_in_cache,
)

logger = logging.getLogger("griptape_nodes")


class HuggingFaceRepoParameter(HuggingFaceModelParameter):
    def __init__(  # noqa: PLR0913
        self,
        node: BaseNode,
        repo_ids: list[str],
        parameter_name: str = "model",
        *,
        list_all_models: bool = False,
        deprecated_repo_ids: list[str] | None = None,
        gated: bool | None = None,
    ):
        # Set before super().__init__, which queries policy and reads these via
        # offers_only_declared_repos() / filter_choices().
        deprecated_repo_ids = deprecated_repo_ids or []
        self._repo_ids = repo_ids + deprecated_repo_ids
        self._list_all_models = list_all_models

        super().__init__(node, parameter_name, gated=gated, deprecated_repos=deprecated_repo_ids)

        self.refresh_parameters()

    def fetch_repo_revisions(self) -> list[tuple[str, str]]:
        if self._list_all_models:
            all_revisions = list_all_repo_revisions_in_cache()
            return sorted(all_revisions, key=lambda x: x[0] not in self._repo_ids)
        return [repo_revision for repo in self._repo_ids for repo_revision in list_repo_revisions_in_cache(repo)]

    def offers_only_declared_repos(self) -> bool:
        """False under ``list_all_models``, which offers every repo in the local cache.

        The library author enumerated ``repo_ids``, but this mode surfaces whatever else the artist
        has pulled down, which no catalog could enumerate ahead of time. Refusing those would turn
        most of the dropdown into "not permitted by your license" rows that no policy ever denied.
        """
        return not self._list_all_models

    def add_input_parameters(self) -> None:
        """Override to apply deprecated model filtering after parameter creation."""
        super().add_input_parameters()
        self.refresh_parameters()

    def get_download_commands(self) -> list[str]:
        return [f'huggingface-cli download "{repo}"' for repo in self.get_download_models()]

    def get_download_models(self) -> list[str]:
        """Returns a list of model names that should be downloaded (excluding deprecated models).

        Strips any `::<subname>` postfix used by providers to encode a sub-model selector within a repo
        (e.g. `Lightricks/LTX-2::ltx-2-19b-dev`). The postfix is not part of the HuggingFace repo ID,
        so it must be removed before the name reaches the model manager UI or the download path.

        The `::` convention is produced by the LTX-2 diffusion pipeline in
        `griptape-nodes-library-advanced-media` — see the LTX-2 `models.py` / `text2vid_parameters.py` /
        `img2vid_parameters.py` where the postfix is generated and later split to select a variant
        subfolder within the shared repo.
        """
        seen: set[str] = set()
        downloads: list[str] = []
        for repo in self._repo_ids:
            if self._is_deprecated(repo):
                continue
            base_repo = repo.split("::", 1)[0]
            if base_repo in seen:
                continue
            seen.add(base_repo)
            downloads.append(base_repo)
        return downloads
