"""Generate the configuration reference doc from the Settings model.

Renders docs/reference/configuration_reference.md from Settings.model_json_schema() so the
per-setting reference (type, default, env var, description) cannot drift from the
code. Run via `make docs/settings-reference`; `make docs` runs it before building.

The page is grouped by the category attached to each Field (see the custom Field
wrapper in settings.py). A top-level scalar setting gets a `GTN_CONFIG_<NAME>` env var
column. A nested setting (a model, e.g. worker/agent/library) gets one env var per scalar
leaf found anywhere beneath it, named with the `__`-separated path
config_manager._load_config_from_env_vars parses. A mapping-valued setting (`type: object`
with `additionalProperties`, e.g. artifacts, project_workspaces) gets a `__<KEY>` template,
since any individual entry can be set that way. Arrays get "n/a": there is no string form
the Settings model accepts for a list.

Nested settings are also expanded one level, so `logging.log_to_file` gets a row of its own
next to its parent, carrying the `GTN_CONFIG_LOGGING__LOG_TO_FILE` form of its env var. One
level is where the keys people look up live; anything deeper is still reachable from the
environment and is listed in its parent's env var column rather than growing the table.
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

    rows = _build_rows(properties, defs, dumped_defaults)
    markdown = _render_markdown(rows)
    # Match the repo's `make check` formatter (mdformat with the gfm plugin) so the
    # committed file is byte-identical to what the format check expects.
    formatted = mdformat.text(markdown, extensions=["gfm"])
    OUTPUT_PATH.write_text(formatted, encoding="utf-8")


def _build_rows(properties: dict, defs: dict, dumped_defaults: dict) -> list[SettingRow]:
    """Build a row per top-level setting, each followed by the sub-keys of its nested model."""
    rows: list[SettingRow] = []
    for name, prop in properties.items():
        row = _build_row(name, prop, defs, dumped_defaults)
        rows.append(row)
        rows.extend(_build_child_rows(name, prop, defs, dumped_defaults, parent=row))
    return rows


def _build_child_rows(
    name: str, prop: dict, defs: dict, dumped_defaults: dict, *, parent: SettingRow
) -> list[SettingRow]:
    """Build a row per sub-key of a nested model, or nothing when the setting is not one."""
    ref = _extract_ref(prop)
    if ref is None:
        return []

    child_properties = defs.get(ref, {}).get("properties", {})
    if not child_properties:
        return []

    child_defaults = dumped_defaults.get(name)
    if not isinstance(child_defaults, dict):
        child_defaults = {}

    rows = []
    for child_name, child_prop in child_properties.items():
        child = _build_row(child_name, child_prop, defs, child_defaults)
        rows.append(
            SettingRow(
                name=f"{name}.{child_name}",
                type_label=child.type_label,
                default_label=child.default_label,
                # Named from the path rather than from the sub-key alone: a sub-key is set
                # from the environment as `GTN_CONFIG_<PARENT>__<CHILD>`, and the same
                # resolver the top-level rows use then spells the mapping and deeper-model
                # forms the same way it does there.
                env_var_label=_resolve_env_var_label(
                    f"{name}__{child_name}", child_prop, defs, is_nested=_is_nested(child_prop, defs)
                ),
                description=child.description,
                # Grouped with its parent rather than under its own category, so the
                # sub-keys of one setting stay together in the table.
                category_name=parent.category_name,
                category_description=parent.category_description,
            )
        )
    return rows


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
    env_var_label = _resolve_env_var_label(name, prop, defs, is_nested=is_nested)
    description = _resolve_description(prop, defs, is_nested=is_nested)

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


def _is_scalar_leaf(prop: dict, defs: dict) -> bool:
    """True when a sub-field is a single value rather than a nested model, list, or mapping.

    An enum $ref has no "properties" key, so it counts as scalar. A union is scalar when any
    non-null member is, since that member's string form is what an env var supplies: `str | None`
    is settable, `list[str] | dict[str, str]` is not.
    """
    if prop.get("type") in {"object", "array"}:
        return False

    if (options := prop.get("anyOf")) is not None:
        return any(option.get("type") != "null" and _is_scalar_leaf(option, defs) for option in options)

    ref = _extract_ref(prop)
    if ref is not None:
        target = defs.get(ref, {})
        return "properties" not in target
    return True


def _scalar_leaf_paths(ref: str, defs: dict, seen: frozenset[str] = frozenset()) -> list[str]:
    """Collect the `__`-joined path to every scalar leaf beneath the model `ref` points at.

    Recurses to full depth, so a scalar buried under an otherwise list-valued model (e.g.
    app_events.on_app_initialization_complete.requires_engine) is still reported. `seen` guards
    against a self-referential schema looping the docs build.
    """
    if ref in seen:
        return []

    paths = []
    sub_properties = defs.get(ref, {}).get("properties", {})
    for sub_name, sub_prop in sub_properties.items():
        if _is_scalar_leaf(sub_prop, defs):
            paths.append(sub_name.upper())
            continue
        sub_ref = _extract_ref(sub_prop)
        if sub_ref is None:
            continue
        paths.extend(
            f"{sub_name.upper()}__{nested_path}" for nested_path in _scalar_leaf_paths(sub_ref, defs, seen | {ref})
        )

    return paths


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

    if isinstance(default_value, list) and default_value:
        # Some of these hold twenty-odd entries, which would swamp the table.
        return f"(list of {len(default_value)} values)"

    if is_nested and default_value not in ([], {}):
        return "(nested object)"

    return f"`{json.dumps(default_value)}`"


def _is_mapping(prop: dict) -> bool:
    """True when a property is a mapping (`type: object` with `additionalProperties`).

    Distinguishes a mapping from a nested model, which comes through a `$ref` to a def with
    `properties` and never carries `additionalProperties` itself, and from an array
    (`type: array`). A mapping's individual entries are settable via a `__<KEY>` env var path
    (config_manager._load_config_from_env_vars splits on `__` and merges into a dict
    regardless of whether the model declares that key), so it is not "n/a" the way an array is.
    """
    return prop.get("type") == "object" and "additionalProperties" in prop


def _resolve_env_var_label(name: str, prop: dict, defs: dict, *, is_nested: bool) -> str:
    if not is_nested:
        return f"`GTN_CONFIG_{name.upper()}`"

    if _is_mapping(prop):
        return f"`GTN_CONFIG_{name.upper()}__<KEY>`"

    ref = _extract_ref(prop)
    if ref is None:
        return "n/a (list/object; edit config file)"

    leaf_paths = _scalar_leaf_paths(ref, defs)
    if not leaf_paths:
        return "n/a (list/object; edit config file)"

    return ", ".join(f"`GTN_CONFIG_{name.upper()}__{leaf_path}`" for leaf_path in leaf_paths)


def _resolve_description(prop: dict, defs: dict, *, is_nested: bool) -> str:
    description = _normalize_cell(prop.get("description", ""))
    if description:
        return description
    if not is_nested:
        return ""

    if prop.get("type") == "array":
        return "A list of values; edit it in a config file."

    if _is_mapping(prop):
        return (
            "Nested settings; an entry can be set with a `GTN_CONFIG_<NAME>__<KEY>` env var "
            "(only reaches an entry whose key is already lowercase), or edited directly in a config file."
        )

    ref = _extract_ref(prop)
    if ref is not None and _scalar_leaf_paths(ref, defs):
        return "Nested settings; the listed sub-keys can be set from the environment, and every sub-key can be edited directly in a config file."
    return "Nested settings; edit the sub-keys directly in a config file."


def _normalize_cell(text: str) -> str:
    """Flatten a value for a single markdown table cell."""
    collapsed = " ".join(text.split())
    return collapsed.replace("|", "\\|")


def _render_markdown(rows: list[SettingRow]) -> str:
    lines = [BANNER, "# Configuration Reference", ""]
    lines.append(
        "Every Griptape Nodes engine setting, grouped by category. Each setting can be placed in any "
        "`griptape_nodes_config.json` file (see [Engine Configuration](../guides/configuration.md) for the load "
        "order). Settings with a `GTN_CONFIG_*` env var, including the `GTN_CONFIG_<NAME>__<SUB_KEY>` form for a "
        "nested setting's scalar sub-keys and the `GTN_CONFIG_<NAME>__<KEY>` form for a mapping-valued setting's "
        "entries, can also be overridden from the environment; list-valued settings must be edited in a config "
        "file. A mapping's keys are matched case-sensitively but the whole variable name is lowercased, so only "
        "an already-lowercase key is reachable from the environment (see the guide for details)."
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
