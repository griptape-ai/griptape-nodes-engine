"""Tests for project template layering and merge functionality."""

import pytest

from griptape_nodes.common.project_templates import (
    DEFAULT_PROJECT_TEMPLATE,
    DEFAULT_PROJECT_TEMPLATE_V0,
    DEFAULT_PROJECT_TEMPLATE_V1,
    ProjectOverrideAction,
    ProjectOverrideCategory,
    ProjectTemplate,
    ProjectValidationInfo,
    ProjectValidationStatus,
    default_template_for_version,
    load_partial_project_template,
    load_project_template_from_yaml,
)

# Use system defaults directly (no longer loading from YAML)
_SYSTEM_DEFAULTS = DEFAULT_PROJECT_TEMPLATE


class TestPartialLoading:
    """Tests for load_partial_project_template function."""

    def test_minimal_valid_overlay(self) -> None:
        """Test loading minimal valid overlay with just name and schema version."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Test Project"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)

        assert overlay is not None
        assert validation.status == ProjectValidationStatus.GOOD
        assert overlay.name == "Test Project"
        assert overlay.project_template_schema_version == "0.1.0"
        assert overlay.situations == {}
        assert overlay.directories == {}
        assert overlay.environment == {}
        assert overlay.description is None

    def test_overlay_with_custom_situation(self) -> None:
        """Test loading overlay with custom situation definition."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  my_situation:
    macro: "{outputs}/custom.{file_extension}"
    policy:
      on_collision: "overwrite"
      create_dirs: true
    fallback: null
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)

        assert overlay is not None
        assert validation.status == ProjectValidationStatus.GOOD
        assert overlay.name == "Custom Project"
        assert "my_situation" in overlay.situations
        assert overlay.situations["my_situation"]["macro"] == "{outputs}/custom.{file_extension}"

    def test_overlay_with_custom_directory(self) -> None:
        """Test loading overlay with custom directory definition."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  custom_dir:
    path_macro: "my_custom_path"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)

        assert overlay is not None
        assert validation.status == ProjectValidationStatus.GOOD
        assert "custom_dir" in overlay.directories
        assert overlay.directories["custom_dir"]["path_macro"] == "my_custom_path"

    def test_overlay_with_environment(self) -> None:
        """Test loading overlay with environment variables."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
environment:
  MY_VAR: "my_value"
  ANOTHER_VAR: "another_value"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)

        assert overlay is not None
        assert validation.status == ProjectValidationStatus.GOOD
        assert overlay.environment["MY_VAR"] == "my_value"
        assert overlay.environment["ANOTHER_VAR"] == "another_value"

    def test_overlay_missing_name(self) -> None:
        """Test that missing name field causes validation error."""
        yaml_text = """
project_template_schema_version: "0.1.0"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        load_partial_project_template(yaml_text, validation)

        assert validation.status == ProjectValidationStatus.UNUSABLE
        assert any("name" in p.field_path for p in validation.problems)

    def test_overlay_missing_schema_version(self) -> None:
        """Test that missing schema version causes validation error."""
        yaml_text = """
name: "Test Project"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        load_partial_project_template(yaml_text, validation)

        assert validation.status == ProjectValidationStatus.UNUSABLE
        assert any("project_template_schema_version" in p.field_path for p in validation.problems)

    def test_overlay_invalid_yaml_syntax(self) -> None:
        """Test that invalid YAML syntax is caught."""
        yaml_text = """
name: "Test Project
project_template_schema_version: "0.1.0"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)

        assert overlay is None
        assert validation.status == ProjectValidationStatus.UNUSABLE
        assert any("YAML syntax error" in p.message for p in validation.problems)


class TestMerge:
    """Tests for ProjectTemplate.merge functionality."""

    def test_merge_minimal_overlay(self) -> None:
        """Test merging minimal overlay with just name override."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "My Custom Project"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        merge_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=merge_validation,
        )

        assert merge_validation.status == ProjectValidationStatus.GOOD
        assert merged.name == "My Custom Project"
        # Should inherit all situations from base
        assert len(merged.situations) == len(default_template.situations)
        # Should inherit all directories from base
        assert len(merged.directories) == len(default_template.directories)

    def test_merge_override_existing_situation(self) -> None:
        """Test merging overlay that modifies an existing situation."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  save_node_output:
    macro: "{outputs}/custom_{node_name}.{file_extension}"
    policy:
      on_collision: "overwrite"
      create_dirs: true
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check situation was modified
        assert merged.situations["save_node_output"].macro == "{outputs}/custom_{node_name}.{file_extension}"
        # Check other situations are inherited
        assert "save_file" in merged.situations
        assert "copy_external_file" in merged.situations

    def test_merge_add_new_situation(self) -> None:
        """Test merging overlay that adds a brand new situation."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  my_new_situation:
    macro: "{outputs}/new_{file_name}.{file_extension}"
    policy:
      on_collision: "create_new"
      create_dirs: true
    fallback: "save_file"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check new situation was added
        assert "my_new_situation" in merged.situations
        assert merged.situations["my_new_situation"].macro == "{outputs}/new_{file_name}.{file_extension}"
        # Check base situations are still there
        assert len(merged.situations) == len(default_template.situations) + 1

    def test_merge_partial_situation_override(self) -> None:
        """Test merging overlay that only overrides macro, inherits other fields."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  save_node_output:
    macro: "{outputs}/different_schema.{file_extension}"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check macro was overridden
        assert merged.situations["save_node_output"].macro == "{outputs}/different_schema.{file_extension}"
        # Check policy was inherited from base
        base_policy = default_template.situations["save_node_output"].policy
        merged_policy = merged.situations["save_node_output"].policy
        assert merged_policy.on_collision == base_policy.on_collision
        assert merged_policy.create_dirs == base_policy.create_dirs

    def test_merge_override_directory(self) -> None:
        """Test merging overlay that overrides an existing directory."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  outputs:
    path_macro: "my_custom_outputs"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check directory was overridden
        assert merged.directories["outputs"].path_macro == "my_custom_outputs"
        # Check other directories are inherited
        assert "inputs" in merged.directories

    def test_merge_add_new_directory(self) -> None:
        """Test merging overlay that adds a new directory."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  custom_dir:
    path_macro: "path/to/custom"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check new directory was added
        assert "custom_dir" in merged.directories
        assert merged.directories["custom_dir"].path_macro == "path/to/custom"
        # Check base directories are still there
        assert len(merged.directories) == len(default_template.directories) + 1

    def test_merge_directory_description_overrides_base(self) -> None:
        """Overlay description on an existing directory replaces the base description."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  outputs:
    description: "Custom description for outputs."
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        default_template = _SYSTEM_DEFAULTS
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Description was overridden
        assert merged.directories["outputs"].description == "Custom description for outputs."
        # Path was inherited from base (overlay didn't set it)
        assert merged.directories["outputs"].path_macro == default_template.directories["outputs"].path_macro

    def test_merge_directory_description_inherited_when_omitted(self) -> None:
        """Overlay that omits description preserves the base description."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  outputs:
    path_macro: "renders"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        default_template = _SYSTEM_DEFAULTS
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Path was overridden, description was inherited from base
        assert merged.directories["outputs"].path_macro == "renders"
        assert merged.directories["outputs"].description == default_template.directories["outputs"].description

    def test_merge_directory_description_explicit_null_clears(self) -> None:
        """Explicit null description in overlay clears an inherited description."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  outputs:
    description: null
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        default_template = _SYSTEM_DEFAULTS
        # Sanity-check: base has a description for outputs, otherwise this test
        # would not actually exercise the clear path.
        assert default_template.directories["outputs"].description is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        assert merged.directories["outputs"].description is None
        assert merged.directories["outputs"].path_macro == default_template.directories["outputs"].path_macro

    def test_merge_new_directory_with_description(self) -> None:
        """A new overlay directory carries its description through to the merged result."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  deliverables:
    path_macro: "client_deliverables"
    description: "Files staged for client delivery."
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        default_template = _SYSTEM_DEFAULTS
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        assert merged.directories["deliverables"].path_macro == "client_deliverables"
        assert merged.directories["deliverables"].description == "Files staged for client delivery."

    def test_merge_environment_variables(self) -> None:
        """Test merging environment variables."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
environment:
  NEW_VAR: "new_value"
  ANOTHER_VAR: "another_value"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        merged = ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check new env vars were added
        assert merged.environment["NEW_VAR"] == "new_value"
        assert merged.environment["ANOTHER_VAR"] == "another_value"


class TestOverrideTracking:
    """Tests for override tracking during merge."""

    def test_track_metadata_name_override(self) -> None:
        """Test that name override is always tracked as MODIFIED."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check name override was tracked
        name_overrides = [
            o for o in validation.overrides if o.category == ProjectOverrideCategory.METADATA and o.name == "name"
        ]
        assert len(name_overrides) == 1
        assert name_overrides[0].action == ProjectOverrideAction.MODIFIED

    def test_track_situation_modified(self) -> None:
        """Test that modifying existing situation is tracked as MODIFIED."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  save_file:
    macro: "{outputs}/different.{file_extension}"
    policy:
      on_collision: "fail"
      create_dirs: false
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check situation override was tracked
        sit_overrides = [
            o for o in validation.overrides if o.category == ProjectOverrideCategory.SITUATION and o.name == "save_file"
        ]
        assert len(sit_overrides) == 1
        assert sit_overrides[0].action == ProjectOverrideAction.MODIFIED

    def test_track_situation_added(self) -> None:
        """Test that adding new situation is tracked as ADDED."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  brand_new_situation:
    macro: "{outputs}/new.{file_extension}"
    policy:
      on_collision: "create_new"
      create_dirs: true
    fallback: null
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check situation addition was tracked
        sit_overrides = [
            o
            for o in validation.overrides
            if o.category == ProjectOverrideCategory.SITUATION and o.name == "brand_new_situation"
        ]
        assert len(sit_overrides) == 1
        assert sit_overrides[0].action == ProjectOverrideAction.ADDED

    def test_track_directory_modified(self) -> None:
        """Test that modifying existing directory is tracked as MODIFIED."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  inputs:
    path_macro: "custom_inputs"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check directory override was tracked
        dir_overrides = [
            o for o in validation.overrides if o.category == ProjectOverrideCategory.DIRECTORY and o.name == "inputs"
        ]
        assert len(dir_overrides) == 1
        assert dir_overrides[0].action == ProjectOverrideAction.MODIFIED

    def test_track_directory_added(self) -> None:
        """Test that adding new directory is tracked as ADDED."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
directories:
  new_directory:
    path_macro: "path/to/new"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check directory addition was tracked
        dir_overrides = [
            o
            for o in validation.overrides
            if o.category == ProjectOverrideCategory.DIRECTORY and o.name == "new_directory"
        ]
        assert len(dir_overrides) == 1
        assert dir_overrides[0].action == ProjectOverrideAction.ADDED

    def test_track_environment_modified(self) -> None:
        """Test that modifying existing env var is tracked as MODIFIED."""
        # First create a base with an env var
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
environment:
  EXISTING_VAR: "modified_value"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        # Create a base template with the env var
        base_with_env = ProjectTemplate(
            project_template_schema_version="0.1.0",
            name="Base",
            situations=default_template.situations,
            directories=default_template.directories,
            environment={"EXISTING_VAR": "original_value"},
            description=None,
        )

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=base_with_env,
            overlay=overlay,
            validation_info=validation,
        )

        # Check env var override was tracked
        env_overrides = [
            o
            for o in validation.overrides
            if o.category == ProjectOverrideCategory.ENVIRONMENT and o.name == "EXISTING_VAR"
        ]
        assert len(env_overrides) == 1
        assert env_overrides[0].action == ProjectOverrideAction.MODIFIED

    def test_track_environment_added(self) -> None:
        """Test that adding new env var is tracked as ADDED."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
environment:
  NEW_VAR: "new_value"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check env var addition was tracked
        env_overrides = [
            o for o in validation.overrides if o.category == ProjectOverrideCategory.ENVIRONMENT and o.name == "NEW_VAR"
        ]
        assert len(env_overrides) == 1
        assert env_overrides[0].action == ProjectOverrideAction.ADDED

    def test_track_multiple_overrides(self) -> None:
        """Test tracking multiple overrides in a single merge."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
description: "Custom description"
situations:
  save_file:
    macro: "{custom}.{file_extension}"
    policy:
      on_collision: "overwrite"
      create_dirs: true
  new_situation:
    macro: "{new}.{file_extension}"
    policy:
      on_collision: "create_new"
      create_dirs: true
    fallback: null
directories:
  outputs:
    path_macro: "custom_outputs"
  new_dir:
    path_macro: "new_directory"
environment:
  VAR1: "value1"
  VAR2: "value2"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Count overrides by category
        metadata_overrides = [o for o in validation.overrides if o.category == ProjectOverrideCategory.METADATA]
        situation_overrides = [o for o in validation.overrides if o.category == ProjectOverrideCategory.SITUATION]
        directory_overrides = [o for o in validation.overrides if o.category == ProjectOverrideCategory.DIRECTORY]
        env_overrides = [o for o in validation.overrides if o.category == ProjectOverrideCategory.ENVIRONMENT]

        assert len(metadata_overrides) == 2  # name + description  # noqa: PLR2004
        assert len(situation_overrides) == 2  # 1 modified + 1 added  # noqa: PLR2004
        assert len(directory_overrides) == 2  # 1 modified + 1 added  # noqa: PLR2004
        assert len(env_overrides) == 2  # 2 added  # noqa: PLR2004

        # Check actions
        assert any(o.action == ProjectOverrideAction.MODIFIED for o in situation_overrides)
        assert any(o.action == ProjectOverrideAction.ADDED for o in situation_overrides)


class TestValidationDuringMerge:
    """Tests for validation errors during merge."""

    def test_invalid_new_situation_schema(self) -> None:
        """Test that invalid macro in new situation causes validation error."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  bad_situation:
    macro: "{unclosed"
    policy:
      on_collision: "create_new"
      create_dirs: true
    fallback: null
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check validation error was recorded
        assert validation.status == ProjectValidationStatus.UNUSABLE
        assert any("macro" in p.field_path.lower() for p in validation.problems)

    def test_incomplete_policy_in_override(self) -> None:
        """Test that incomplete policy in situation override causes validation error."""
        yaml_text = """
project_template_schema_version: "0.1.0"
name: "Custom Project"
situations:
  save_file:
    policy:
      on_collision: "overwrite"
"""
        overlay_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, overlay_validation)
        assert overlay is not None

        ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        default_template = _SYSTEM_DEFAULTS
        assert default_template is not None

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        ProjectTemplate.merge(
            base=default_template,
            overlay=overlay,
            validation_info=validation,
        )

        # Check validation error for incomplete policy
        assert validation.status == ProjectValidationStatus.UNUSABLE
        assert any("policy" in p.field_path and "both" in p.message.lower() for p in validation.problems)


class TestProjectTemplateToYaml:
    """Tests for ProjectTemplate.to_yaml()."""

    def test_to_yaml_contains_required_top_level_fields(self) -> None:
        yaml_str = DEFAULT_PROJECT_TEMPLATE.to_yaml()

        # The dumper quotes all string scalars, including keys.
        assert '"project_template_schema_version":' in yaml_str
        assert '"name":' in yaml_str
        assert '"situations":' in yaml_str
        assert '"directories":' in yaml_str

    def test_to_yaml_excludes_none_description(self) -> None:
        template = ProjectTemplate(
            project_template_schema_version=ProjectTemplate.LATEST_SCHEMA_VERSION,
            name="x",
            situations={},
            directories={},
            description=None,
        )

        assert "description" not in template.to_yaml()

    def test_to_yaml_strips_nested_name_keys(self) -> None:
        # Loader injects `name` into nested situations/directories from their dict keys,
        # so emitting `name:` inside those nested objects would duplicate on round-trip.
        yaml_str = DEFAULT_PROJECT_TEMPLATE.to_yaml()

        # Only the top-level `name:` (with no indentation) should appear.
        indented_name_lines = [
            line for line in yaml_str.splitlines() if line.lstrip().startswith("name:") and line != line.lstrip()
        ]
        assert indented_name_lines == []

    def test_to_yaml_round_trip(self) -> None:
        yaml_str = DEFAULT_PROJECT_TEMPLATE.to_yaml()

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        loaded = load_project_template_from_yaml(yaml_str, validation)

        assert loaded is not None
        assert validation.status == ProjectValidationStatus.GOOD
        assert loaded.name == DEFAULT_PROJECT_TEMPLATE.name
        assert loaded.project_template_schema_version == DEFAULT_PROJECT_TEMPLATE.project_template_schema_version
        assert set(loaded.situations.keys()) == set(DEFAULT_PROJECT_TEMPLATE.situations.keys())
        assert set(loaded.directories.keys()) == set(DEFAULT_PROJECT_TEMPLATE.directories.keys())

    def test_to_yaml_larger_than_overlay_against_self(self) -> None:
        # Overlay against self contains only the two required fields; the full
        # dump always contains every section, so must be strictly longer.
        overlay = DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        full = DEFAULT_PROJECT_TEMPLATE.to_yaml()

        assert len(full) > len(overlay)

    def test_overlay_emits_full_policy_when_only_one_field_differs(self) -> None:
        # The loader treats `policy` as atomic: SituationTemplate.merge rejects any overlay policy
        # that does not contain both on_collision and create_dirs. If the overlay writer emitted
        # only the field that differs from base, the round-tripped file would be UNUSABLE.
        # Flip create_dirs on save_file only; on_collision matches base.
        base_sit = DEFAULT_PROJECT_TEMPLATE.situations["save_file"]
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={
                "situations": {
                    **DEFAULT_PROJECT_TEMPLATE.situations,
                    "save_file": base_sit.model_copy(
                        update={
                            "policy": base_sit.policy.model_copy(
                                update={"create_dirs": not base_sit.policy.create_dirs}
                            )
                        }
                    ),
                }
            }
        )

        overlay_yaml = modified.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)

        # Round-trip through the loader: the overlay alone must validate (it gets merged on load),
        # and the merged template must match our in-memory modification.
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        assert validation.status == ProjectValidationStatus.GOOD

        merged = ProjectTemplate.merge(base=DEFAULT_PROJECT_TEMPLATE, overlay=overlay_data, validation_info=validation)
        assert validation.status == ProjectValidationStatus.GOOD
        assert merged.situations["save_file"].policy.create_dirs == (not base_sit.policy.create_dirs)
        assert merged.situations["save_file"].policy.on_collision == base_sit.policy.on_collision


class TestDefaultProjectTemplate:
    """Tests for the content of the default project template."""

    def test_save_temp_file_situation_exists(self) -> None:
        assert "save_temp_file" in DEFAULT_PROJECT_TEMPLATE.situations

    def test_save_temp_file_situation_uses_overwrite_policy(self) -> None:
        from griptape_nodes.common.project_templates.situation import SituationFilePolicy

        situation = DEFAULT_PROJECT_TEMPLATE.situations["save_temp_file"]
        assert situation.policy.on_collision == SituationFilePolicy.OVERWRITE

    def test_save_temp_file_situation_creates_dirs(self) -> None:
        situation = DEFAULT_PROJECT_TEMPLATE.situations["save_temp_file"]
        assert situation.policy.create_dirs is True

    def test_save_temp_file_situation_falls_back_to_save_file(self) -> None:
        situation = DEFAULT_PROJECT_TEMPLATE.situations["save_temp_file"]
        assert situation.fallback == "save_file"

    def test_save_temp_file_macro_uses_temp_directory(self) -> None:
        situation = DEFAULT_PROJECT_TEMPLATE.situations["save_temp_file"]
        assert situation.macro.startswith("{temp}/")


class TestOverlayDeletions:
    """Overlay/merge symmetry for deletions: removed base items must stay removed on reload."""

    def _roundtrip(self, modified: ProjectTemplate) -> ProjectTemplate:
        overlay_yaml = modified.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        assert validation.status == ProjectValidationStatus.GOOD
        merged = ProjectTemplate.merge(base=DEFAULT_PROJECT_TEMPLATE, overlay=overlay_data, validation_info=validation)
        assert validation.status == ProjectValidationStatus.GOOD
        return merged

    def test_removed_base_situation_stays_removed(self) -> None:
        removed_name = next(iter(DEFAULT_PROJECT_TEMPLATE.situations))
        remaining = {k: v for k, v in DEFAULT_PROJECT_TEMPLATE.situations.items() if k != removed_name}
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"situations": remaining})

        merged = self._roundtrip(modified)

        assert removed_name not in merged.situations

    def test_removed_base_directory_stays_removed(self) -> None:
        removed_name = next(iter(DEFAULT_PROJECT_TEMPLATE.directories))
        remaining = {k: v for k, v in DEFAULT_PROJECT_TEMPLATE.directories.items() if k != removed_name}
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"directories": remaining})

        merged = self._roundtrip(modified)

        assert removed_name not in merged.directories

    def test_removed_environment_var_stays_removed(self) -> None:
        seeded = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"environment": {"FOO": "bar", "BAZ": "qux"}})
        overlay_yaml = seeded.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        round_tripped = ProjectTemplate.merge(
            base=DEFAULT_PROJECT_TEMPLATE, overlay=overlay_data, validation_info=validation
        )
        assert round_tripped.environment == {"FOO": "bar", "BAZ": "qux"}

        # Now remove FOO and ensure the overlay tombstones it so merge drops it.
        trimmed = round_tripped.model_copy(update={"environment": {"BAZ": "qux"}})
        merged = self._roundtrip(trimmed)

        assert "FOO" not in merged.environment
        assert merged.environment.get("BAZ") == "qux"

    def test_cleared_description_stays_cleared(self) -> None:
        seeded = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"description": "previous description"})
        cleared = seeded.model_copy(update={"description": None})

        overlay_yaml = cleared.to_overlay_yaml(seeded)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        merged = ProjectTemplate.merge(base=seeded, overlay=overlay_data, validation_info=validation)

        assert merged.description is None

    def test_removed_situation_records_removed_override(self) -> None:
        removed_name = next(iter(DEFAULT_PROJECT_TEMPLATE.situations))
        remaining = {k: v for k, v in DEFAULT_PROJECT_TEMPLATE.situations.items() if k != removed_name}
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"situations": remaining})

        overlay_yaml = modified.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        ProjectTemplate.merge(base=DEFAULT_PROJECT_TEMPLATE, overlay=overlay_data, validation_info=validation)

        removed = [
            o
            for o in validation.overrides
            if o.category == ProjectOverrideCategory.SITUATION
            and o.name == removed_name
            and o.action == ProjectOverrideAction.REMOVED
        ]
        assert len(removed) == 1


class TestProjectIdAndParentId:
    """Round-trip and merge behavior for the opaque `id` and id-based parent link.

    `id` is identity: it is always emitted (never diffed away) and the child's
    own id always wins on merge (never inherited). `parent_project_id` is the
    portable parent link that supersedes the legacy, machine-specific
    `parent_project_path` (engine#4806).
    """

    def _roundtrip(
        self,
        modified: ProjectTemplate,
        base: ProjectTemplate = DEFAULT_PROJECT_TEMPLATE,
    ) -> ProjectTemplate:
        overlay_yaml = modified.to_overlay_yaml(base)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        assert validation.status == ProjectValidationStatus.GOOD
        merged = ProjectTemplate.merge(base=base, overlay=overlay_data, validation_info=validation)
        assert validation.status == ProjectValidationStatus.GOOD
        return merged

    def test_id_survives_overlay_round_trip(self) -> None:
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "my-guid"})

        merged = self._roundtrip(modified)

        assert merged.id == "my-guid"

    def test_id_always_emitted_even_when_matching_base(self) -> None:
        # id is identity, not a diff: it must be emitted whenever present, even if
        # the (unusual) base carries the same id. The default base has id=None, so
        # use a base that already declares the same id to exercise the "matches
        # base" path.
        base = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "same-guid"})
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "same-guid"})

        overlay_yaml = modified.to_overlay_yaml(base)

        assert '"id":' in overlay_yaml
        assert "same-guid" in overlay_yaml

    def test_parent_project_id_survives_overlay_round_trip(self) -> None:
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "child-guid", "parent_project_id": "parent-guid"})

        merged = self._roundtrip(modified)

        assert merged.id == "child-guid"
        assert merged.parent_project_id == "parent-guid"
        # The id-based link must not also carry a legacy path.
        assert merged.parent_project_path is None

    def test_new_save_emits_parent_project_id_not_path(self) -> None:
        # engine#4806: a child of a non-default parent emits the portable id, never
        # the author's machine-specific path, so a coworker can open the shared file.
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "child-guid", "parent_project_id": "parent-guid"})

        overlay_yaml = modified.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)

        assert "parent_project_id" in overlay_yaml
        assert "parent_project_path" not in overlay_yaml

    def test_parent_project_id_wins_over_path_on_emit(self) -> None:
        # parent_project_id and parent_project_path are mutually exclusive on emit:
        # when both are set, only the id is written.
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={
                "id": "child-guid",
                "parent_project_id": "parent-guid",
                "parent_project_path": "/abs/parent/griptape-nodes-project.yml",
            }
        )

        overlay_yaml = modified.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)

        assert "parent_project_id" in overlay_yaml
        assert "parent_project_path" not in overlay_yaml

    def test_clears_parent_project_id_tombstone(self) -> None:
        # An explicit `parent_project_id: null` overlay tombstones an inherited
        # id-based link. The loader records the clear; merge yields None (the link
        # is never inherited from base regardless, but the tombstone is honored).
        base = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"parent_project_id": "parent-guid"})
        yaml_text = """
project_template_schema_version: "0.3.3"
name: "Child"
parent_project_id: null
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(yaml_text, validation)
        assert overlay_data is not None
        assert overlay_data.clears_parent_project_id is True
        assert overlay_data.parent_project_id is None

        merged = ProjectTemplate.merge(base=base, overlay=overlay_data, validation_info=validation)

        assert merged.parent_project_id is None

    def test_legacy_parent_project_path_still_round_trips(self) -> None:
        # Backwards compat: a legacy child with no id and a parent_project_path must
        # still emit and re-parse the path (the id-based emit must not shadow it).
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={"parent_project_path": "/abs/parent/griptape-nodes-project.yml"}
        )

        merged = self._roundtrip(modified)

        assert merged.parent_project_path == "/abs/parent/griptape-nodes-project.yml"
        assert merged.parent_project_id is None

    def test_to_yaml_includes_id(self) -> None:
        template = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"id": "my-guid"})

        yaml_str = template.to_yaml()

        assert '"id":' in yaml_str
        assert "my-guid" in yaml_str

    def test_to_yaml_omits_none_id(self) -> None:
        # to_yaml uses exclude_none, so a template with no id (the default) must not
        # emit an id key.
        assert '"id":' not in DEFAULT_PROJECT_TEMPLATE.to_yaml()


class TestWorkspaceDir:
    """Round-trip, tombstone, and merge behavior for the per-project `workspace_dir` field.

    `workspace_dir` declares the workspace a project uses. It is the highest-priority
    workspace source at resolution time, but as a template field it has OWN-node merge
    semantics (never inherited from base, mirroring the parent link) and the stored value
    is the raw string/per-platform mapping (never absolutized) so a relative path stays
    portable across machines.
    """

    def _roundtrip(
        self,
        modified: ProjectTemplate,
        base: ProjectTemplate = DEFAULT_PROJECT_TEMPLATE,
    ) -> ProjectTemplate:
        overlay_yaml = modified.to_overlay_yaml(base)
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(overlay_yaml, validation)
        assert overlay_data is not None
        assert validation.status == ProjectValidationStatus.GOOD
        merged = ProjectTemplate.merge(base=base, overlay=overlay_data, validation_info=validation)
        assert validation.status == ProjectValidationStatus.GOOD
        return merged

    def test_string_workspace_dir_survives_round_trip(self) -> None:
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"workspace_dir": "/abs/workspace"})

        merged = self._roundtrip(modified)

        assert merged.workspace_dir == "/abs/workspace"

    def test_relative_workspace_dir_stored_verbatim(self) -> None:
        # A relative value is persisted as-is (never absolutized): resolution to an
        # absolute path happens only at resolve time, keeping the project portable.
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"workspace_dir": "./workspace"})

        overlay_yaml = modified.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        assert "workspace_dir" in overlay_yaml

        merged = self._roundtrip(modified)
        assert merged.workspace_dir == "./workspace"

    def test_per_platform_workspace_dir_survives_round_trip(self) -> None:
        from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath

        per_platform = PerPlatformProjectPath(linux="/ws/linux", darwin="/ws/darwin", windows="C:\\ws", default="/ws")
        modified = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"workspace_dir": per_platform})

        merged = self._roundtrip(modified)

        assert isinstance(merged.workspace_dir, PerPlatformProjectPath)
        assert merged.workspace_dir.linux == "/ws/linux"
        assert merged.workspace_dir.darwin == "/ws/darwin"
        assert merged.workspace_dir.windows == "C:\\ws"
        assert merged.workspace_dir.default == "/ws"

    def test_unset_workspace_dir_not_emitted(self) -> None:
        # The default has workspace_dir=None; an unset field must not be written.
        assert "workspace_dir" not in DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)

    def test_explicit_null_tombstones_inherited_workspace_dir(self) -> None:
        # An explicit `workspace_dir: null` overlay clears an inherited value.
        base = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"workspace_dir": "/inherited/ws"})
        yaml_text = """
project_template_schema_version: "0.4.1"
name: "Child"
workspace_dir: null
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(yaml_text, validation)
        assert overlay_data is not None
        assert overlay_data.clears_workspace_dir is True
        assert overlay_data.workspace_dir is None

        merged = ProjectTemplate.merge(base=base, overlay=overlay_data, validation_info=validation)

        assert merged.workspace_dir is None

    def test_child_does_not_inherit_base_workspace_dir(self) -> None:
        # OWN-node semantics: a child whose overlay omits workspace_dir does NOT
        # adopt the base's value (cross-project inheritance is the resolution
        # ladder's job, not merge's).
        base = DEFAULT_PROJECT_TEMPLATE.model_copy(update={"workspace_dir": "/base/ws"})
        yaml_text = """
project_template_schema_version: "0.4.1"
name: "Child"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay_data = load_partial_project_template(yaml_text, validation)
        assert overlay_data is not None
        assert overlay_data.workspace_dir is None
        assert overlay_data.clears_workspace_dir is False

        merged = ProjectTemplate.merge(base=base, overlay=overlay_data, validation_info=validation)

        assert merged.workspace_dir is None

    def test_invalid_workspace_dir_type_records_error(self) -> None:
        # A non-string, non-mapping workspace_dir is a structural error.
        yaml_text = """
project_template_schema_version: "0.4.1"
name: "Bad"
workspace_dir: 42
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        load_partial_project_template(yaml_text, validation)

        assert validation.status != ProjectValidationStatus.GOOD
        assert any("workspace_dir" in problem.field_path for problem in validation.problems)


class TestSchemaVersionSavePolicy:
    """Version stamped on save: auto-upgrade within a major, preserve across a major.

    Within the same major as LATEST, a save advances the version to LATEST (minor/patch
    bumps are additive). Across a major boundary the loaded version is preserved verbatim,
    so a v0 project never silently becomes v1 (which would adopt a new defaults baseline).
    LATEST is pinned via monkeypatch so the policy is tested independently of its current
    value.
    """

    def _template(self, version: str) -> ProjectTemplate:
        return DEFAULT_PROJECT_TEMPLATE.model_copy(update={"project_template_schema_version": version})

    def test_within_major_upgrades_to_latest_on_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ProjectTemplate, "LATEST_SCHEMA_VERSION", "1.4.0")
        overlay_yaml = self._template("1.1.0").to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        assert '"project_template_schema_version": "1.4.0"' in overlay_yaml

    def test_across_major_preserves_loaded_on_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ProjectTemplate, "LATEST_SCHEMA_VERSION", "1.4.0")
        overlay_yaml = self._template("0.5.2").to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        assert '"project_template_schema_version": "0.5.2"' in overlay_yaml

    def test_already_latest_is_unchanged_on_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ProjectTemplate, "LATEST_SCHEMA_VERSION", "1.4.0")
        overlay_yaml = self._template("1.4.0").to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        assert '"project_template_schema_version": "1.4.0"' in overlay_yaml

    def test_within_major_upgrades_to_latest_on_full_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ProjectTemplate, "LATEST_SCHEMA_VERSION", "1.4.0")
        yaml_str = self._template("1.0.0").to_yaml()
        assert '"project_template_schema_version": "1.4.0"' in yaml_str

    def test_across_major_preserves_loaded_on_full_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ProjectTemplate, "LATEST_SCHEMA_VERSION", "1.4.0")
        yaml_str = self._template("0.5.2").to_yaml()
        assert '"project_template_schema_version": "0.5.2"' in yaml_str


class TestDefaultTemplateByMajor:
    """default_template_for_version selects the merge-base default by schema MAJOR.

    A v0 project merges onto the frozen v0 baseline (workspace-root-relative dirs); a v1
    project merges onto the v1 baseline (workflow-relative dirs, file_extension_directory
    routing, 3d/splat categories). This is what lets the breaking default change land under
    a new major without moving existing v0 projects.
    """

    def test_v0_version_selects_v0_default(self) -> None:
        assert default_template_for_version("0.5.1") is DEFAULT_PROJECT_TEMPLATE_V0

    def test_v1_version_selects_v1_default(self) -> None:
        assert default_template_for_version("1.0.0") is DEFAULT_PROJECT_TEMPLATE_V1

    def test_latest_alias_is_v1(self) -> None:
        assert DEFAULT_PROJECT_TEMPLATE is DEFAULT_PROJECT_TEMPLATE_V1

    def test_unknown_future_major_falls_back_to_latest(self) -> None:
        # A project declaring a not-yet-known major still loads against the newest baseline.
        assert default_template_for_version("9.9.9") is DEFAULT_PROJECT_TEMPLATE

    def test_v0_default_is_workspace_root_relative(self) -> None:
        # The frozen v0 layout: dirs are NOT workflow-relative and routing is absent.
        assert DEFAULT_PROJECT_TEMPLATE_V0.directories["inputs"].path_macro == "inputs"

    def test_v1_default_is_workflow_relative(self) -> None:
        assert DEFAULT_PROJECT_TEMPLATE_V1.directories["inputs"].path_macro == "{workflow_dir?:/}inputs"

    def test_v0_project_merges_onto_v0_layout(self) -> None:
        # A v0 overlay with no directory override inherits the v0 (workspace-root) layout,
        # NOT the v1 workflow-relative one.
        yaml_text = """
project_template_schema_version: "0.5.1"
name: "Legacy Project"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)
        assert overlay is not None
        merged = ProjectTemplate.merge(
            base=default_template_for_version(overlay.project_template_schema_version),
            overlay=overlay,
            validation_info=validation,
        )
        assert merged.directories["inputs"].path_macro == "inputs"

    def test_v1_project_merges_onto_v1_layout(self) -> None:
        yaml_text = """
project_template_schema_version: "1.0.0"
name: "Modern Project"
"""
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)
        assert overlay is not None
        merged = ProjectTemplate.merge(
            base=default_template_for_version(overlay.project_template_schema_version),
            overlay=overlay,
            validation_info=validation,
        )
        assert merged.directories["inputs"].path_macro == "{workflow_dir?:/}inputs"
