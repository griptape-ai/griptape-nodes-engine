"""Resolve a project situation name into file-write configuration."""

import logging
import typing

from griptape_nodes.common.project_templates import situation as situation_mod
from griptape_nodes.retained_mode import griptape_nodes as griptape_nodes_mod
from griptape_nodes.retained_mode.events import os_events, project_events

logger = logging.getLogger("griptape_nodes")

SITUATION_TO_FILE_POLICY: dict[str, os_events.ExistingFilePolicy] = {
    situation_mod.SituationFilePolicy.CREATE_NEW: os_events.ExistingFilePolicy.CREATE_NEW,
    situation_mod.SituationFilePolicy.OVERWRITE: os_events.ExistingFilePolicy.OVERWRITE,
    situation_mod.SituationFilePolicy.FAIL: os_events.ExistingFilePolicy.FAIL,
    situation_mod.SituationFilePolicy.PROMPT: os_events.ExistingFilePolicy.CREATE_NEW,  # PROMPT has no direct mapping; fall back to CREATE_NEW
}


class ResolvedSituation(typing.NamedTuple):
    """Result of looking up a project situation by name.

    Attributes:
        macro_template: The macro template string for the situation.
        existing_file_policy: Mapped file collision policy.
        create_parents: Whether to create intermediate directories.
        situation_obj: Raw situation template, or None when the lookup failed and
            fallback values are in use.
    """

    macro_template: str
    existing_file_policy: os_events.ExistingFilePolicy
    create_parents: bool
    situation_obj: situation_mod.SituationTemplate | None


def resolve_situation(
    situation_name: str,
    fallback_macro: str,
    default_policy: os_events.ExistingFilePolicy = os_events.ExistingFilePolicy.CREATE_NEW,
) -> ResolvedSituation:
    """Look up a situation by name and return its resolved configuration.

    Falls back to fallback_macro and default_policy when the situation cannot be loaded.

    Args:
        situation_name: Situation name to look up in the current project.
        fallback_macro: Macro template to use when the situation cannot be found.
        default_policy: ExistingFilePolicy to use in the fallback case.

    Returns:
        ResolvedSituation with macro_template, existing_file_policy, create_parents,
        and situation_obj (None when falling back).
    """
    result = griptape_nodes_mod.GriptapeNodes.handle_request(
        project_events.GetSituationRequest(situation_name=situation_name)
    )
    if isinstance(result, project_events.GetSituationResultSuccess):
        situation_obj = result.situation
        return ResolvedSituation(
            macro_template=situation_obj.macro,
            existing_file_policy=SITUATION_TO_FILE_POLICY.get(situation_obj.policy.on_collision, default_policy),
            create_parents=situation_obj.policy.create_dirs,
            situation_obj=situation_obj,
        )
    logger.error("Failed to load situation '%s', using fallback macro template", situation_name)
    return ResolvedSituation(
        macro_template=fallback_macro,
        existing_file_policy=default_policy,
        create_parents=True,
        situation_obj=None,
    )
