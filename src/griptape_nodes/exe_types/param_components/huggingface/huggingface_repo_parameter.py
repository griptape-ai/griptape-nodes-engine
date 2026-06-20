import logging

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.huggingface.huggingface_model_parameter import HuggingFaceModelParameter
from griptape_nodes.exe_types.param_components.huggingface.huggingface_utils import (
    list_all_repo_revisions_in_cache,
    list_repo_revisions_in_cache,
)
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")


class HuggingFaceRepoParameter(HuggingFaceModelParameter):
    def __init__(
        self,
        node: BaseNode,
        repo_ids: list[str],
        parameter_name: str = "model",
        *,
        list_all_models: bool = False,
        deprecated_repo_ids: list[str] | None = None,
    ):
        super().__init__(node, parameter_name)

        deprecated_repo_ids = deprecated_repo_ids or []
        self._deprecated_repos = deprecated_repo_ids

        self._repo_ids = repo_ids + deprecated_repo_ids
        self._list_all_models = list_all_models
        self.refresh_parameters()

    def fetch_repo_revisions(self) -> list[tuple[str, str]]:
        if self._list_all_models:
            all_revisions = list_all_repo_revisions_in_cache()
            return sorted(all_revisions, key=lambda x: x[0] not in self._repo_ids)
        return [repo_revision for repo in self._repo_ids for repo_revision in list_repo_revisions_in_cache(repo)]

    def _is_deprecated(self, repo: str) -> bool:
        return repo in self._deprecated_repos

    def refresh_parameters(self, value_being_set: str | None = None) -> None:
        """Override to filter deprecated models except the currently selected one.

        Args:
            value_being_set: Optional value that's being set (used during after_value_set)
        """
        parameter = self._node.get_parameter_by_name(self._parameter_name)
        if parameter is None:
            logger.debug(
                "Parameter '%s' not found on node '%s'; cannot refresh choices.",
                self._parameter_name,
                self._node.name,
            )
            return

        # Get all cached models
        all_choices = self.get_choices()
        if not all_choices:
            super().refresh_parameters()
            return

        # Get current value - use value_being_set if provided (during after_value_set)
        current_value = (
            value_being_set if value_being_set is not None else self._node.get_parameter_value(self._parameter_name)
        )

        # Filter: include non-deprecated models, and deprecated model if it's currently selected
        filtered_choices = []
        for choice in all_choices:
            repo_id, _ = self._key_to_repo_revision(choice)
            is_deprecated = self._is_deprecated(repo_id)

            # Include if: not deprecated OR matches current/being-set value
            if not is_deprecated or choice == current_value:
                filtered_choices.append(choice)

        # If no choices after filtering, include all (initial state)
        if not filtered_choices:
            super().refresh_parameters()
            return

        # Determine default value
        if current_value and current_value in filtered_choices:
            default_value = current_value
        else:
            default_value = filtered_choices[0]

        if parameter.find_elements_by_type(Options):
            self._node._update_option_choices(self._parameter_name, filtered_choices, default_value)
        else:
            parameter.add_trait(Options(choices=filtered_choices))

        self._apply_data_choices(parameter)
        self._update_download_button(default_value, parameter)

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
