import logging
import re
from abc import ABC, abstractmethod

from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload, OnClickMessageResultPayload
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

    @property
    def _download_param_name(self) -> str:
        return f"{self._parameter_name}_download"

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

        self._apply_data_choices(parameter)
        self._update_download_button_visibility()

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

        self._apply_data_choices(parameter)

        download_button = ParameterString(
            name=self._download_param_name,
            default_value="",
            display_name="",
            traits={
                Button(
                    label="Open Model Manager to Download",
                    icon="download",
                    variant="secondary",
                    full_width=True,
                    on_click=self._on_download_click,
                ),
            },
            tooltip="Open Model Manager to download the selected model",
            allowed_modes={ParameterMode.PROPERTY},
            accept_any=False,
        )
        self._node.add_parameter(download_button)
        self._node.hide_parameter_by_name(self._download_param_name)

    def remove_input_parameters(self) -> None:
        self._node.remove_parameter_element_by_name(self._parameter_name)
        self._node.remove_parameter_element_by_name(self._download_param_name)

    def get_choices(self) -> list[str]:
        self._repo_revisions = self.fetch_repo_revisions()

        model_counts: dict[str, int] = {}
        for repo_id, _ in self.list_repo_revisions():
            model_counts[repo_id] = model_counts.get(repo_id, 0) + 1

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
                data.append({"name": choice, "icon": "check-circle", "subtitle": "Downloaded"})
            elif choice in not_downloaded or repo_id in not_downloaded:
                data.append({"name": choice, "icon": "download", "subtitle": "Not downloaded"})
            else:
                data.append({"name": choice})
        return data

    def _apply_data_choices(self, parameter: Parameter) -> None:
        parameter.update_ui_options(
            {
                "data": self._build_data_choices(),
                "dropdown_row_icons": True,
                "dropdown_row_subtitles": True,
            }
        )

    def _get_model_search_term(self, choice: str) -> str:
        repo_id, _ = self._key_to_repo_revision(choice)
        return repo_id

    def _update_download_button_visibility(self) -> None:
        if self._node.get_parameter_by_name(self._download_param_name) is None:
            return
        has_missing = bool(self.get_not_downloaded_choices())
        if has_missing:
            self._node.show_parameter_by_name(self._download_param_name)
        else:
            self._node.hide_parameter_by_name(self._download_param_name)

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

        repo_id, revision = self._key_to_repo_revision(value)

        if not revision:
            for stored_repo_id, stored_revision in self._repo_revisions:
                if stored_repo_id == repo_id:
                    logger.debug("Using revision '%s' for model '%s'", stored_revision, repo_id)
                    return stored_repo_id, stored_revision
            msg = f"Model '{repo_id}' not found in available models!"
            raise RuntimeError(msg)

        return repo_id, revision

    def _on_refresh_click(
        self, _button: Button, _button_details: ButtonDetailsMessagePayload
    ) -> NodeMessageResult | None:
        self.refresh_parameters()
        return None

    def _on_download_click(
        self, _button: Button, button_details: ButtonDetailsMessagePayload
    ) -> NodeMessageResult | None:
        value = self._node.get_parameter_value(self._parameter_name)
        search_term = self._get_model_search_term(str(value))
        return NodeMessageResult(
            success=True,
            details="Opening Model Manager",
            response=OnClickMessageResultPayload(
                button_details=button_details,
                href=f"#model-management?search={search_term}",
            ),
            altered_workflow_state=False,
        )

    @abstractmethod
    def fetch_repo_revisions(self) -> list[tuple[str, str]]: ...

    @abstractmethod
    def get_download_commands(self) -> list[str]: ...

    @abstractmethod
    def get_download_models(self) -> list[str]: ...
