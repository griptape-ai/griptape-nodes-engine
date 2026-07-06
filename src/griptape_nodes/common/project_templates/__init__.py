"""Project template system for managing project.yml files and situations."""

from griptape_nodes.common.project_templates.default_project_template import (
    DEFAULT_PROJECT_TEMPLATE,
    DEFAULT_PROJECT_TEMPLATE_V0,
    DEFAULT_PROJECT_TEMPLATE_V1,
    default_template_for_version,
    schema_major_or_none,
)
from griptape_nodes.common.project_templates.directory import DirectoryDefinition, PerPlatformPathMacro
from griptape_nodes.common.project_templates.loader import (
    ProjectOverlayData,
    YAMLLineInfo,
    YAMLParseResult,
    load_partial_project_template,
    load_project_template_from_yaml,
    load_yaml_with_line_tracking,
)
from griptape_nodes.common.project_templates.project import ProjectTemplate
from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath, select_project_path
from griptape_nodes.common.project_templates.situation import (
    SituationFilePolicy,
    SituationPolicy,
    SituationTemplate,
)
from griptape_nodes.common.project_templates.validation import (
    ProjectOverride,
    ProjectOverrideAction,
    ProjectOverrideCategory,
    ProjectValidationInfo,
    ProjectValidationProblem,
    ProjectValidationProblemSeverity,
    ProjectValidationStatus,
)

__all__ = [
    "DEFAULT_PROJECT_TEMPLATE",
    "DEFAULT_PROJECT_TEMPLATE_V0",
    "DEFAULT_PROJECT_TEMPLATE_V1",
    "DirectoryDefinition",
    "PerPlatformPathMacro",
    "PerPlatformProjectPath",
    "ProjectOverlayData",
    "ProjectOverride",
    "ProjectOverrideAction",
    "ProjectOverrideCategory",
    "ProjectTemplate",
    "ProjectValidationInfo",
    "ProjectValidationProblem",
    "ProjectValidationProblemSeverity",
    "ProjectValidationStatus",
    "SituationFilePolicy",
    "SituationPolicy",
    "SituationTemplate",
    "YAMLLineInfo",
    "YAMLParseResult",
    "default_template_for_version",
    "load_partial_project_template",
    "load_project_template_from_yaml",
    "load_yaml_with_line_tracking",
    "schema_major_or_none",
    "select_project_path",
]
