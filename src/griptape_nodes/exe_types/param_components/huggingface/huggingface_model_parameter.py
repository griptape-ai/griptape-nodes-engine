import logging
import re
from abc import ABC, abstractmethod

from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

_NO_MODELS_PLACEHOLDER = "No models downloaded — visit Model Manager"


class HuggingFaceModelParameter(ABC):
    @classmethod
    def _repo_revision_to_key(cls, repo_revision: tuple[str, str]) -> str:
        return f"{repo_revision[0]} ({repo_revision[1]})"

    @classmethod
    def _key_to_repo_revision(cls, key: str) -> tuple[str, str]:
        # Check if key has hash format using regex
        hash_pattern = r"^(.+) \(([a-f0-9]{40})\)$"
        match = re.match(hash_pattern, key)
        if match:
            return match.group(1), match.group(2)

        # Key is just the model name (no hash)
        return key, ""

    def __init__(self, node: BaseNode, parameter_name: str):
        self._node = node
        self._parameter_name = parameter_name
        self._repo_revisions = []
        self._download_button: Button | None = None

    def refresh_parameters(self) -> None:
        parameter = self._node.get_parameter_by_name(self._parameter_name)
        if parameter is None:
            logger.debug(
                "Parameter '%s' not found on node '%s'; cannot refresh choices.",
                self._parameter_name,
                self._node.name,
            )
            return

        choices = self.get_choices()

        if choices:
            default_value = choices[0]
            display_choices = choices
        else:
            default_value = _NO_MODELS_PLACEHOLDER
            display_choices = [_NO_MODELS_PLACEHOLDER]

        if parameter.find_elements_by_type(Options):
            self._node._update_option_choices(self._parameter_name, display_choices, default_value)
        else:
            parameter.add_trait(Options(choices=display_choices))

        self._node.set_parameter_value(self._parameter_name, default_value)

        parameter.ui_options["data"] = self._build_data_choices()
        self._update_download_button(default_value, parameter)

    def add_input_parameters(self) -> None:
        choices = self.get_choices()

        display_choices = choices or [_NO_MODELS_PLACEHOLDER]
        default_value = choices[0] if choices else _NO_MODELS_PLACEHOLDER

        parameter = ParameterString(
            name=self._parameter_name,
            default_value=default_value,
            display_name=self._parameter_name,
            traits={
                Options(choices=display_choices),
                Button(
                    icon="list-restart",
                    size="icon",
                    variant="secondary",
                    on_click=self._on_refresh_click,
                ),
            },
            tooltip=self._parameter_name,
            allowed_modes={ParameterMode.PROPERTY},
            accept_any=False,
        )

        self._node.add_parameter(parameter)
        self._node.set_parameter_value(self._parameter_name, default_value, initial_setup=True)

        parameter.ui_options["data"] = self._build_data_choices()
        self._update_download_button(default_value, parameter)

    def remove_input_parameters(self) -> None:
        self._node.remove_parameter_element_by_name(self._parameter_name)

    def get_choices(self) -> list[str]:
        # Ensure the latest repo revisions are fetched
        self._repo_revisions = self.fetch_repo_revisions()

        # Count occurrences of each model name to detect duplicates
        model_counts: dict[str, int] = {}
        for repo_id, _ in self.list_repo_revisions():
            model_counts[repo_id] = model_counts.get(repo_id, 0) + 1

        # Generate keys for downloaded models (hash only when there are duplicates)
        downloaded_choices = []
        for repo_revision in self.list_repo_revisions():
            repo_id, _ = repo_revision
            if model_counts[repo_id] > 1:
                downloaded_choices.append(self._repo_revision_to_key(repo_revision))
            else:
                downloaded_choices.append(repo_id)

        not_downloaded = self.get_not_downloaded_choices()

        all_choices = downloaded_choices + not_downloaded
        logger.debug("Available choices for parameter '%s': %s", self._parameter_name, all_choices)
        return all_choices

    def get_not_downloaded_choices(self) -> list[str]:
        downloaded_repo_ids = {repo_id for repo_id, _ in self.list_repo_revisions()}
        return [m for m in self.get_download_models() if m not in downloaded_repo_ids]

    def _build_data_choices(self) -> list[dict]:
        downloaded_keys = {repo_id for repo_id, _ in self.list_repo_revisions()}
        not_downloaded = set(self.get_not_downloaded_choices())
        choices = self.get_choices()

        data = []
        for choice in choices:
            repo_id, _ = self._key_to_repo_revision(choice)
            if repo_id in downloaded_keys or choice in downloaded_keys:
                data.append({"name": choice, "args": {}, "icon": "check-circle", "subtitle": "Downloaded"})
            elif choice in not_downloaded or repo_id in not_downloaded:
                data.append({"name": choice, "args": {}, "icon": "download", "subtitle": "Not downloaded"})
            else:
                data.append({"name": choice, "args": {}})
        return data

    def _get_model_search_term(self, choice: str) -> str:
        repo_id, _ = self._key_to_repo_revision(choice)
        return repo_id

    def validate_before_node_run(self) -> list[Exception] | None:
        self.refresh_parameters()
        try:
            self.get_repo_revision()
        except Exception as e:
            return [e]

        return None

    def list_repo_revisions(self) -> list[tuple[str, str]]:
        return self._repo_revisions

    def get_repo_revision(self) -> tuple[str, str]:
        value = self._node.get_parameter_value(self._parameter_name)
        if value is None:
            msg = "Model download required!"
            raise RuntimeError(msg)

        # Parse the value using _key_to_repo_revision
        repo_id, revision = self._key_to_repo_revision(value)

        # If revision is empty (just model name), find it in our stored list
        if not revision:
            for stored_repo_id, stored_revision in self._repo_revisions:
                if stored_repo_id == repo_id:
                    logger.debug("Using revision '%s' for model '%s'", stored_revision, repo_id)
                    return stored_repo_id, stored_revision
            # If not found, raise an error
            msg = f"Model '{repo_id}' not found in available models!"
            raise RuntimeError(msg)

        # If revision was provided, return it directly
        return repo_id, revision

    def after_value_set(self, parameter: Parameter, value: object) -> None:
        if parameter.name != self._parameter_name:
            return
        self._update_download_button(value, parameter)

    def _update_download_button(self, value: object, parameter: Parameter) -> None:
        downloaded_keys = {repo_id for repo_id, _ in self.list_repo_revisions()}

        # Determine if the selected value is a downloaded model
        is_downloaded = value in downloaded_keys or str(value) in downloaded_keys
        if not is_downloaded:
            repo_id, _ = self._key_to_repo_revision(str(value)) if value else ("", "")
            is_downloaded = repo_id in downloaded_keys

        value_is_placeholder = value == _NO_MODELS_PLACEHOLDER or value is None

        if not is_downloaded and not value_is_placeholder:
            # Remove old button if present
            if self._download_button is not None:
                parameter.remove_trait(self._download_button)
                self._download_button = None

            search_term = self._get_model_search_term(str(value))
            button = Button(
                icon="download",
                size="icon",
                variant="secondary",
                tooltip="Open in Model Manager to download",
                button_link=f"#model-management?search={search_term}",
            )
            parameter.add_trait(button)
            self._download_button = button
        elif self._download_button is not None:
            parameter.remove_trait(self._download_button)
            self._download_button = None

    def _on_refresh_click(
        self, _button: Button, _button_details: ButtonDetailsMessagePayload
    ) -> NodeMessageResult | None:
        self.refresh_parameters()
        return None

    @abstractmethod
    def fetch_repo_revisions(self) -> list[tuple[str, str]]: ...

    @abstractmethod
    def get_download_commands(self) -> list[str]: ...

    @abstractmethod
    def get_download_models(self) -> list[str]: ...
