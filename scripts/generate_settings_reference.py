"""Generate the configuration reference doc from the Settings model.

Renders docs/reference/configuration_reference.md from Settings.model_json_schema() so the
per-setting reference (type, default, env var, description) cannot drift from the
code. Run via `make docs/settings-reference`; `make docs` runs it before building.

The page is grouped by the category attached to each Field (see the custom Field
wrapper in settings.py). Only top-level scalar settings get a GTN_CONFIG_* env var
column, because env-var parsing is a flat key lookup with no nesting support
(config_manager._load_config_from_env_vars).
"""

import json
from dataclasses import dataclass
from pathlib import Path

import mdformat

from griptape_nodes.retained_mode.managers.settings import Settings

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration_reference.md"

BANNER = (
    "<!-- GENERATED FILE - DO NOT EDIT BY HAND.\n"
    "     Regenerate with `make docs/settings-reference` after changing the Settings model. -->\n"
)

# workspace_directory's default is computed from the current working directory at import
# time, so render a stable placeholder instead of the machine-specific absolute path.
CWD_DEPENDENT_DEFAULTS = {"workspace_directory": "<current_working_directory>/GriptapeNodes"}


@dataclass
class SettingRow:
    name: str
    type_label: str
    default_label: str
    env_var_label: str
    description: str
    category_name: str
    category_description: str


def generate() -> None:
    """Render the configuration reference markdown from the Settings schema."""
    schema = Settings.model_json_schema()
    properties = schema.get("properties", {})
    defs = schema.get("$defs", {})
    dumped_defaults = Settings().model_dump()

    rows = [_build_row(name, prop, defs, dumped_defaults) for name, prop in properties.items()]
    markdown = _render_markdown(rows)
    # Match the repo's `make check` formatter (mdformat with the gfm plugin) so the
    # committed file is byte-identical to what the format check expects.
    formatted = mdformat.text(markdown, extensions=["gfm"])
    OUTPUT_PATH.write_text(formatted, encoding="utf-8")


def _build_row(name: str, prop: dict, defs: dict, dumped_defaults: dict) -> SettingRow:
    category = prop.get("category", {})
    if isinstance(category, str):
        category_name = category
        category_description = ""
    else:
        category_name = category.get("name", "General")
        category_description = category.get("description", "")

    is_nested = _is_nested(prop, defs)
    type_label = _resolve_type_label(prop, defs)
    default_label = _resolve_default_label(name, dumped_defaults, is_nested=is_nested)
    env_var_label = _resolve_env_var_label(name, is_nested=is_nested)
    description = _resolve_description(prop, is_nested=is_nested)

    return SettingRow(
        name=name,
        type_label=type_label,
        default_label=default_label,
        env_var_label=env_var_label,
        description=description,
        category_name=category_name,
        category_description=category_description,
    )


def _is_nested(prop: dict, defs: dict) -> bool:
    """True when the setting is a nested model, list, or mapping (not a scalar)."""
    if prop.get("type") in {"object", "array"}:
        return True
    ref = _extract_ref(prop)
    if ref is not None:
        target = defs.get(ref, {})
        return "properties" in target
    return False


def _resolve_type_label(prop: dict, defs: dict) -> str:
    if "const" in prop:
        return f"`{json.dumps(prop['const'])}` (constant)"

    if "enum" in prop:
        return _enum_label(prop["enum"])

    ref = _extract_ref(prop)
    if ref is not None:
        return _ref_type_label(ref, defs)

    if "anyOf" in prop:
        return _any_of_label(prop["anyOf"], defs)

    return prop.get("type", "any")


def _ref_type_label(ref: str, defs: dict) -> str:
    target = defs.get(ref, {})
    if "enum" in target:
        return _enum_label(target["enum"])
    if "properties" in target:
        return "object"
    return ref


def _enum_label(values: list) -> str:
    rendered = ", ".join(f"`{value}`" for value in values)
    return f"one of {rendered}"


def _any_of_label(any_of: list, defs: dict) -> str:
    labels = []
    for option in any_of:
        if option.get("type") == "null":
            continue
        labels.append(_resolve_type_label(option, defs))
    if not labels:
        return "any"
    return " or ".join(labels)


def _resolve_default_label(name: str, dumped_defaults: dict, *, is_nested: bool) -> str:
    if name in CWD_DEPENDENT_DEFAULTS:
        return f"`{CWD_DEPENDENT_DEFAULTS[name]}`"

    default_value = dumped_defaults.get(name)

    if is_nested and default_value not in ([], {}):
        return "(nested object)"

    return f"`{json.dumps(default_value)}`"


def _resolve_env_var_label(name: str, *, is_nested: bool) -> str:
    if is_nested:
        return "n/a (nested; edit config file)"
    return f"`GTN_CONFIG_{name.upper()}`"


def _resolve_description(prop: dict, *, is_nested: bool) -> str:
    description = _normalize_cell(prop.get("description", ""))
    if description:
        return description
    if is_nested:
        return "Nested settings; edit the sub-keys directly in a config file."
    return ""


def _normalize_cell(text: str) -> str:
    """Flatten a value for a single markdown table cell."""
    collapsed = " ".join(text.split())
    return collapsed.replace("|", "\\|")


def _render_markdown(rows: list[SettingRow]) -> str:
    lines = [BANNER, "# Configuration Reference", ""]
    lines.append(
        "Every Griptape Nodes engine setting, grouped by category. Each setting can be placed in any "
        "`griptape_nodes_config.json` file (see [Engine Configuration](../guides/configuration.md) for the load "
        "order). Settings with a `GTN_CONFIG_*` env var can also be overridden from the environment; "
        "nested settings must be edited in a config file."
    )
    lines.append("")

    for category_name in _ordered_categories(rows):
        category_rows = [row for row in rows if row.category_name == category_name]
        lines.append(f"## {category_name}")
        lines.append("")
        category_description = category_rows[0].category_description
        if category_description:
            lines.append(category_description)
            lines.append("")
        lines.append("| Setting | Type | Default | Environment variable | Description |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.extend(
            f"| `{row.name}` | {row.type_label} | {row.default_label} | {row.env_var_label} | {row.description} |"
            for row in category_rows
        )
        lines.append("")

    return "\n".join(lines) + "\n"


def _ordered_categories(rows: list[SettingRow]) -> list[str]:
    """Category names in first-appearance order (declaration order in the model)."""
    ordered: list[str] = []
    for row in rows:
        if row.category_name not in ordered:
            ordered.append(row.category_name)
    return ordered


def _extract_ref(prop: dict) -> str | None:
    ref = prop.get("$ref")
    if ref is None and "allOf" in prop and len(prop["allOf"]) == 1:
        ref = prop["allOf"][0].get("$ref")
    if ref is None:
        return None
    return ref.split("/")[-1]


if __name__ == "__main__":
    generate()
