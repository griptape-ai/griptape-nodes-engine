import logging
import re
from abc import ABC, abstractmethod

from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_types.parameter_button import ParameterButton
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.retained_mode.events.access_events import (
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.model_events import ListModelDownloadsRequest, ListModelDownloadsResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload, OnClickMessageResultPayload
from griptape_nodes.traits.options import Options

logger = logging.getLogger("griptape_nodes")

_NO_MODELS_PLACEHOLDER = "No models downloaded — visit Model Manager"

# Denial decoration. Deliberately identical to ModelAccessComponent's wording so a gated
# HuggingFace dropdown and a gated static dropdown read the same way to an artist.
_DENIED_ROW_ICON = "shield-off"
_DENIED_ROW_SUBTITLE = "Not permitted by your license"
_BADGE_TITLE = "Model Not Permitted"


class HuggingFaceModelParameter(ABC):
    """Mixin component that adds an inline model-selection dropdown to a node.

    The dropdown shows all models — downloaded and not yet downloaded — with
    per-row icons and subtitles ("Downloaded", "Downloading…", "Not downloaded").
    A secondary button appears below the dropdown when the selected model needs
    attention (not downloaded, or currently downloading) and navigates the user
    to the Model Manager.

    Subclasses implement fetch_repo_revisions(), get_download_commands(), and
    get_download_models() to define which models appear in the list.
    """

    @classmethod
    def _repo_revision_to_key(cls, repo_revision: tuple[str, str]) -> str:
        return f"{repo_revision[0]} ({repo_revision[1]})"

    @classmethod
    def _key_to_repo_revision(cls, key: str) -> tuple[str, str]:
        # Keys with multiple cached revisions embed a 40-char hash: "owner/repo (deadbeef…)"
        hash_pattern = r"^(.+) \(([a-f0-9]{40})\)$"
        match = re.match(hash_pattern, key)
        if match:
            return match.group(1), match.group(2)

        return key, ""

    def __init__(self, node: BaseNode, parameter_name: str, *, gated: bool | None = None):
        """Build the component.

        ``gated`` controls license enforcement on the dropdown:

        - ``None`` (default): auto-detect. Gating turns on when the node's library manifest
          declares models for this node type, and stays off otherwise. A library that has
          adopted a ``model_catalog`` therefore gets enforcement without touching its Python,
          and one that has not keeps the historical "offer whatever is cached" behavior.
        - ``True``: always enforce, and refuse every selection if policy cannot be evaluated.
          Use when a caller knows its models are declared and wants the failure to be loud.
        - ``False``: never enforce.
        """
        self._node = node
        self._parameter_name = parameter_name
        self._repo_revisions: list[tuple[str, str]] = []
        # Cached at refresh time only — never fetched from inside a callback to
        # avoid nested GriptapeNodes.handle_request() calls that cause recursion.
        self._downloading_model_ids: set[str] = set()

        # License-policy state, replaced atomically by _refresh_policy() so the two lookup
        # tables can never drift apart.
        self._gate_mode = gated
        self._gated = gated is True
        self._denial_by_repo_id: dict[str, CheckpointDenial] = {}
        self._catalog_id_by_repo_id: dict[str, str] = {}
        self._policy_failure_detail: str | None = None
        if gated is not False:
            self._refresh_policy()

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

        # Snapshot active downloads before rebuilding choices so the dropdown
        # subtitles and button visibility reflect the current download state.
        self._refresh_downloading_model_ids()
        # Re-query policy alongside the cache scan so a license change since the last refresh
        # is reflected in both the row decoration and the badge. Guard on the configured mode,
        # not on `_gated`: under auto-detect `_gated` is the *result* of the query, so keying off
        # it here would leave a first refresh permanently ungated.
        if self._gate_mode is not False:
            self._refresh_policy()
        choices = self.get_choices()

        if choices:
            default_value = self._preferred_default(choices)
            display_choices = choices
        else:
            default_value = _NO_MODELS_PLACEHOLDER
            display_choices = [_NO_MODELS_PLACEHOLDER]

        if parameter.find_elements_by_type(Options):
            self._node._update_option_choices(self._parameter_name, display_choices, default_value)
        else:
            parameter.add_trait(Options(choices=display_choices))

        self._node.set_parameter_value(self._parameter_name, default_value)

        self._apply_data_choices(parameter, display_choices)
        self._apply_denial_badge(parameter, default_value)
        self._update_download_button_visibility()

    def _preferred_default(self, choices: list[str]) -> str:
        """Pick the value to select after a refresh.

        Keeps the artist's current selection when it survived the refresh, so re-scanning the
        cache does not silently retarget a configured node. Falls back to the first choice, and
        when gated prefers the first *permitted* choice so a node does not open on a model the
        license forbids.
        """
        current = self._node.get_parameter_value(self._parameter_name)
        if isinstance(current, str) and current in choices:
            return current
        if self._gated:
            for choice in choices:
                if self.query_for_denial(choice) is None:
                    return choice
        return choices[0]

    def add_input_parameters(self) -> None:
        choices = self.get_choices()

        display_choices = choices or [_NO_MODELS_PLACEHOLDER]
        default_value = self._preferred_default(choices) if choices else _NO_MODELS_PLACEHOLDER

        # Main model dropdown. The refresh button (list-restart) sits inline
        # inside the dropdown row via the Button trait alongside Options.
        # The converter fires on every value change so the download button
        # visibility updates immediately when the user picks a different model.
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
                    tooltip="Refresh model list",
                ),
            },
            tooltip=self._parameter_name,
            allowed_modes={ParameterMode.PROPERTY},
            converters=[self._on_selection_changed],
            accept_any=False,
        )

        self._node.add_parameter(parameter)
        self._node.set_parameter_value(self._parameter_name, default_value, initial_setup=True)

        self._apply_data_choices(parameter, display_choices)
        self._apply_denial_badge(parameter, default_value)

        # Download button starts hidden; _update_download_button_visibility()
        # shows it when the selected model is not downloaded or is downloading.
        download_button = ParameterButton(
            name=self._download_param_name,
            label="Open Model Manager to Download",
            icon="download",
            variant="secondary",
            full_width=True,
            on_click=self._on_download_click,
            tooltip="Open Model Manager to download the selected model",
            hide=True,
            allowed_modes={ParameterMode.PROPERTY},
        )
        self._node.add_parameter(download_button)

    def remove_input_parameters(self) -> None:
        self._node.remove_parameter_element_by_name(self._parameter_name)
        self._node.remove_parameter_element_by_name(self._download_param_name)

    def get_choices(self) -> list[str]:
        self._repo_revisions = self.fetch_repo_revisions()

        # When the same repo has multiple cached revisions, show "repo (hash)"
        # so the user can distinguish them. If there's only one, show just the
        # repo ID for a cleaner display.
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

    def repo_id_for_choice(self, choice: str) -> str | None:
        """Reduce a dropdown choice to the bare HuggingFace repo id the catalog declares.

        The single normalization point for policy lookups. Choice strings are not repo ids: a
        repo with more than one cached revision renders as ``"owner/repo (<40-hex>)"``, and some
        providers append a ``::subvariant`` selector for a sub-model inside a shared repo. Both
        must be stripped before matching against a catalog ``provider_model_id`` or a denied
        model silently reads as unknown. Returns ``None`` for the placeholder row, which is a UI
        affordance rather than a model.
        """
        if not choice or choice == _NO_MODELS_PLACEHOLDER:
            return None
        repo_id, _ = self._key_to_repo_revision(choice)
        return repo_id.split("::", 1)[0]

    def query_for_denial(self, choice: str) -> CheckpointDenial | None:
        """Return the denial for ``choice``, or ``None`` when it is permitted.

        Fails closed. An ungated parameter never denies. A gated one denies when the selection
        does not resolve to a declared catalog model, because an undeclared repo carries no
        ``ModelProvider`` parent edge and therefore cannot be matched by a provider-scoped
        policy -- treating it as permitted would let an encumbered model through by omission.
        """
        if not self._gated:
            return None
        if self._policy_failure_detail is not None:
            return CheckpointDenial(failures=(CheckpointFailure(detail=self._policy_failure_detail),))
        repo_id = self.repo_id_for_choice(choice)
        if repo_id is None:
            return None
        denial = self._denial_by_repo_id.get(repo_id)
        if denial is not None:
            return denial
        if repo_id not in self._catalog_id_by_repo_id:
            return CheckpointDenial(
                failures=(
                    CheckpointFailure(
                        detail=(
                            f"Model '{repo_id}' is not declared in this library's model catalog, so license "
                            "policy cannot be evaluated for it. Add it to the catalog to make it selectable."
                        )
                    ),
                )
            )
        return None

    def raise_if_denied(self, choice: str) -> None:
        """Raise ``RuntimeError`` when ``choice`` is not permitted. For raise-based run paths."""
        denial = self.query_for_denial(choice)
        if denial is None:
            return
        msg = f"Cannot use '{choice}': it is not permitted. {denial.reason()}"
        raise RuntimeError(msg)

    def _refresh_policy(self) -> None:
        """Re-query license policy for this node type and rebuild both lookup tables.

        Replaces the tables together so they cannot drift. On a failed query the tables are
        cleared and ``_policy_failure_detail`` is set, which makes every subsequent
        ``query_for_denial`` deny rather than treating "no denials known" as "no denials".
        """
        node_type = type(self._node).__name__
        result = GriptapeNodes.handle_request(QueryModelAccessForNodeRequest(node_type=node_type))
        if not isinstance(result, QueryModelAccessForNodeResultSuccess):
            details = getattr(result, "result_details", None) or type(result).__name__
            self._denial_by_repo_id = {}
            self._catalog_id_by_repo_id = {}
            if self._gate_mode is None:
                # Auto-detect: an unresolvable node type means this library has not adopted
                # declarations, which is the pre-adoption status quo rather than an error.
                self._gated = False
                self._policy_failure_detail = None
                logger.debug(
                    "Model parameter '%s' on node type '%s' is ungated: %s",
                    self._parameter_name,
                    node_type,
                    details,
                )
                return
            logger.warning(
                "Gated model parameter '%s': engine could not resolve model access for node type '%s' (%s). "
                "Selections will be refused until this resolves. Verify the node's "
                "griptape_nodes_library.json entry declares a model_usage block.",
                self._parameter_name,
                node_type,
                details,
            )
            self._policy_failure_detail = (
                f"License policy could not be evaluated for node '{node_type}' ({details}). "
                "Verify the library manifest declares this node type with a model_usage block."
            )
            return

        denials: dict[str, CheckpointDenial] = {}
        catalog_ids: dict[str, str] = {}
        for verdict in result.verdicts:
            if verdict.provider_model_id is None:
                continue
            repo_id = verdict.provider_model_id.split("::", 1)[0]
            catalog_ids[repo_id] = verdict.model_id
            if verdict.denial is not None:
                denials[repo_id] = verdict.denial
        self._denial_by_repo_id = denials
        self._catalog_id_by_repo_id = catalog_ids
        self._policy_failure_detail = None
        if self._gate_mode is None:
            # Enforce only once the library actually declares models for this node. An empty
            # verdict list means nothing is declared, so there is no catalog to gate against.
            self._gated = bool(catalog_ids)

    def _apply_denial_badge(self, parameter: Parameter, value: str | None = None) -> None:
        """Set or clear the parameter's badge for the current selection."""
        if not self._gated:
            return
        if value is None:
            value = str(self._node.get_parameter_value(self._parameter_name) or "")
        denial = self.query_for_denial(value)
        if denial is None:
            parameter.clear_badge()
            return
        parameter.set_badge(
            variant="error",
            title=_BADGE_TITLE,
            message=f"Model `{value}` is not permitted. Running this node will fail.\n\nReason(s): {denial.reason()}",
            icon=_DENIED_ROW_ICON,
        )

    def _refresh_downloading_model_ids(self) -> None:
        # Only called from refresh_parameters() — never from inside a button
        # callback — to avoid nested handle_request() calls that cause recursion.
        result = GriptapeNodes.handle_request(ListModelDownloadsRequest())
        if not isinstance(result, ListModelDownloadsResultSuccess):
            self._downloading_model_ids = set()
            return
        self._downloading_model_ids = {s.model_id for s in result.downloads if s.status == "downloading"}

    def _build_data_choices(self, choices: list[str]) -> list[dict]:
        downloaded_keys = {repo_id for repo_id, _ in self.list_repo_revisions()}
        not_downloaded = set(self.get_not_downloaded_choices())
        downloading = self._downloading_model_ids

        data = []
        for choice in choices:
            repo_id, _ = self._key_to_repo_revision(choice)
            # Entitlement outranks download status: a model the license forbids is not worth
            # telling the artist to download. This branch is also why denial decoration lives
            # here rather than in a second update_ui_options() writer — `data`,
            # `dropdown_row_icons`, and `dropdown_row_subtitles` have exactly one owner, so a
            # refresh cannot silently erase the other's rows.
            if self._gated and self.query_for_denial(choice) is not None:
                data.append({"name": choice, "icon": _DENIED_ROW_ICON, "subtitle": _DENIED_ROW_SUBTITLE})
            # Downloading check must come before downloaded: HuggingFace creates
            # cache entries as soon as a download starts, so a partially-downloaded
            # model appears in fetch_repo_revisions() and would otherwise show
            # as "Downloaded" while still in progress.
            elif repo_id in downloading or choice in downloading:
                data.append({"name": choice, "icon": "loader", "subtitle": "Downloading…"})
            elif repo_id in downloaded_keys or choice in downloaded_keys:
                data.append({"name": choice, "icon": "check-circle", "subtitle": "Downloaded"})
            elif choice in not_downloaded or repo_id in not_downloaded:
                data.append({"name": choice, "icon": "download", "subtitle": "Not downloaded"})
            else:
                data.append({"name": choice})
        return data

    def _apply_data_choices(self, parameter: Parameter, choices: list[str]) -> None:
        parameter.update_ui_options(
            {
                "data": self._build_data_choices(choices),
                "dropdown_row_icons": True,
                "dropdown_row_subtitles": True,
            }
        )

    def _get_model_search_term(self, choice: str) -> str:
        repo_id, _ = self._key_to_repo_revision(choice)
        return repo_id

    def _on_selection_changed(self, value: object) -> object:
        # Converter attached to the model parameter; fires on every value change
        # so the download button shows/hides as the user switches models.
        self._update_download_button_visibility(str(value))
        # Badge tracks the selection from the cached policy tables — a local lookup, no engine
        # round-trip, so this stays cheap enough for a converter.
        parameter = self._node.get_parameter_by_name(self._parameter_name)
        if parameter is not None:
            self._apply_denial_badge(parameter, str(value))
        return value

    def _update_download_button_visibility(self, value: str | None = None) -> None:
        if self._node.get_parameter_by_name(self._download_param_name) is None:
            return
        if value is None:
            value = str(self._node.get_parameter_value(self._parameter_name) or "")

        not_downloaded = set(self.get_not_downloaded_choices())
        search_term = self._get_model_search_term(value)
        is_downloading = search_term in self._downloading_model_ids
        should_show = value in not_downloaded or is_downloading

        if should_show:
            self._node.show_parameter_by_name(self._download_param_name)
            # Update the button label to match context so the call-to-action
            # makes sense whether the model is queued to download or already downloading.
            download_param = self._node.get_parameter_by_name(self._download_param_name)
            if isinstance(download_param, ParameterButton):
                download_param.label = "View Download Progress" if is_downloading else "Open Model Manager to Download"
        else:
            self._node.hide_parameter_by_name(self._download_param_name)

    def validate_before_node_run(self) -> list[Exception] | None:
        self.refresh_parameters()
        # Gate the run itself, not just the dropdown. Row decoration is advisory -- a workflow
        # can carry a value that was permitted when it was saved, or that never passed through
        # the UI at all -- so the selection is re-checked against current policy here.
        if self._gated:
            selection = self._node.get_parameter_value(self._parameter_name)
            if isinstance(selection, str):
                denial = self.query_for_denial(selection)
                if denial is not None:
                    return [
                        RuntimeError(
                            f"Attempted to use model '{selection}' on node '{self._node.name}'. "
                            f"Failed because it is not permitted by your license. {denial.reason()}"
                        )
                    ]
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
        # Use the cached downloading state — calling handle_request() here would
        # create a nested request inside the button-click handler and cause recursion.
        if search_term in self._downloading_model_ids:
            # Already downloading: open the downloads view pre-filtered to this model.
            href = f"#model-management?filter={search_term}"
        else:
            # Not yet downloaded: open model search so the user can start the download.
            href = f"#model-management?search={search_term}"
        return NodeMessageResult(
            success=True,
            details="Opening Model Manager",
            response=OnClickMessageResultPayload(
                button_details=button_details,
                href=href,
            ),
            altered_workflow_state=False,
        )

    @abstractmethod
    def fetch_repo_revisions(self) -> list[tuple[str, str]]: ...

    @abstractmethod
    def get_download_commands(self) -> list[str]: ...

    @abstractmethod
    def get_download_models(self) -> list[str]: ...
