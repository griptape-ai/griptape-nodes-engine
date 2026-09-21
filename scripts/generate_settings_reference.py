"""Generate the configuration reference doc from the Settings model.

Renders docs/reference/configuration_reference.md from Settings.model_json_schema() so the
per-setting reference (type, default, env var, description) cannot drift from the code. Run via
`make docs/settings-reference`; `make docs` runs it before building.

Each row is keyed by the dotted path a config file uses. A nested model (e.g. `app_events`,
`worker`) is flattened to full depth, so every sub-field gets its own type, default, env var, and
description instead of hiding behind an "(object)" cell. Rows are grouped by the category attached
to each Field (see the custom Field wrapper in settings.py); a nested field inherits its parent's
category unless it declares its own, which is how
`app_events.on_app_initialization_complete.projects_to_register` lands under Projects.

The env var column is the `__`-joined path config_manager._load_config_from_env_vars parses: a
scalar gets `GTN_CONFIG_<PATH>`, a mapping (`type: object` with `additionalProperties`, e.g.
artifacts) gets a `__<KEY>` template since any entry can be set that way, and a list gets "n/a"
because the Settings model accepts no string form for one.

A model reached as a list entry or mapping value (e.g. MCPServerConfig under mcp_servers) has no
dotted key of its own, so it gets a table under Entry Types, linked from the setting's type cell.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import mdformat
from markdown.extensions.toc import slugify

from griptape_nodes.retained_mode.managers.settings import Settings

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration_reference.md"

BANNER = (
    "<!-- GENERATED FILE - DO NOT EDIT BY HAND.\n"
    "     Regenerate with `make docs/settings-reference` after changing the Settings model. -->\n"
)

# workspace_directory's default is computed from the current working directory at import
# time, so render a stable placeholder instead of the machine-specific absolute path.
CWD_DEPENDENT_DEFAULTS = {"workspace_directory": "<current_working_directory>/GriptapeNodes"}

# A long default list (e.g. app_events.events_to_echo_as_retained_mode) would swamp its cell.
MAX_DEFAULT_LIST_ITEMS = 3

NOT_SETTABLE_FROM_ENV = "n/a (edit config file)"

LeafKind = Literal["scalar", "mapping", "other"]


@dataclass(frozen=True)
class Category:
    name: str
    description: str


# The Field wrapper in settings.py defaults every field's category to this name, so a field
# carrying it declared nothing and should inherit the category of the model it sits under.
GENERAL_CATEGORY = Category(name="General", description="")


@dataclass(frozen=True)
class Schema:
    """The Settings schema plus its default values, threaded through the recursive walk."""

    defs: dict
    defaults: dict


@dataclass
class SettingRow:
    path: tuple[str, ...]
    type_label: str
    default_label: str
    env_var_label: str
    description: str
    category: Category
    schema: dict

    @property
    def name(self) -> str:
        return ".".join(self.path)


@dataclass
class FieldRow:
    name: str
    type_label: str
    default_label: str
    description: str


@dataclass
class EntryType:
    title: str
    description: str
    field_rows: list[FieldRow]


def generate() -> None:
    """Render the configuration reference markdown from the Settings schema."""
    json_schema = Settings.model_json_schema()
    schema = Schema(defs=json_schema.get("$defs", {}), defaults=Settings().model_dump())

    rows = _build_rows(
        json_schema.get("properties", {}),
        schema,
        parent_path=(),
        parent_category=GENERAL_CATEGORY,
        seen=frozenset(),
    )
    entry_types = [_build_entry_type(ref, schema.defs) for ref in _ordered_entry_refs(rows, schema.defs)]
    markdown = _render_markdown(rows, entry_types)
    # Match the repo's `make check` formatter (mdformat with the gfm plugin) so the
    # committed file is byte-identical to what the format check expects.
    formatted = mdformat.text(markdown, extensions=["gfm"])
    OUTPUT_PATH.write_text(formatted, encoding="utf-8")


def _build_rows(
    properties: dict,
    schema: Schema,
    *,
    parent_path: tuple[str, ...],
    parent_category: Category,
    seen: frozenset[str],
) -> list[SettingRow]:
    rows = []
    for name, prop in properties.items():
        rows.extend(
            _rows_for_property(
                (*parent_path, name),
                prop,
                schema,
                parent_category=parent_category,
                seen=seen,
            )
        )
    return rows


def _rows_for_property(
    path: tuple[str, ...],
    prop: dict,
    schema: Schema,
    *,
    parent_category: Category,
    seen: frozenset[str],
) -> list[SettingRow]:
    """Rows for one schema property: its sub-fields if it is a nested model, else itself."""
    category = _resolve_category(prop, parent_category)
    description = _normalize_cell(prop.get("description", ""))
    ref = _extract_ref(prop)
    sub_properties = _sub_properties(ref, schema.defs, seen)

    if sub_properties is None:
        return [
            SettingRow(
                path=path,
                type_label=_resolve_type_label(prop, schema.defs),
                default_label=_resolve_default_label(path, schema.defaults),
                env_var_label=_resolve_env_var_label(path, prop, schema.defs),
                description=description,
                category=category,
                schema=prop,
            )
        ]

    rows = []
    # A nested model's own description would otherwise vanish, since the model itself has no row.
    if description:
        rows.append(
            SettingRow(
                path=path,
                type_label="object",
                default_label="(nested object)",
                env_var_label="n/a (see sub-keys)",
                description=description,
                category=category,
                # The model is flattened into the rows below, not an entry type, so this row
                # must not feed its ref to _ordered_entry_refs.
                schema={},
            )
        )
    rows.extend(
        _build_rows(
            sub_properties,
            schema,
            parent_path=path,
            parent_category=category,
            seen=seen | {ref} if ref is not None else seen,
        )
    )
    return rows


def _sub_properties(ref: str | None, defs: dict, seen: frozenset[str]) -> dict | None:
    """The fields of the nested model `ref` points at, or None when it is not one to flatten.

    `seen` guards against a self-referential schema looping the docs build: a model already
    expanded higher in the path is rendered as a single row instead.
    """
    if ref is None or ref in seen:
        return None
    properties = defs.get(ref, {}).get("properties")
    if not properties:
        return None
    return properties


def _resolve_category(prop: dict, parent_category: Category) -> Category:
    category = prop.get("category")
    if category is None:
        return parent_category
    if isinstance(category, str):
        name = category
        description = ""
    else:
        name = category.get("name", GENERAL_CATEGORY.name)
        description = category.get("description", "")
    if name == GENERAL_CATEGORY.name:
        return parent_category
    return Category(name=name, description=description)


def _leaf_kind(prop: dict, defs: dict) -> LeafKind:
    """How a leaf can be supplied from the environment.

    "scalar" is a single value an env var sets directly. "mapping" is a dict whose individual
    entries are settable via a `__<KEY>` path. "other" (a list, or a union of non-scalars) has no
    string form the Settings model accepts.
    """
    if "const" in prop or "enum" in prop:
        return "scalar"

    if (options := prop.get("anyOf")) is not None:
        return _union_leaf_kind(options, defs)

    ref = _extract_ref(prop)
    if ref is not None:
        if "properties" in defs.get(ref, {}):
            return "other"
        return "scalar"

    return _plain_leaf_kind(prop)


def _union_leaf_kind(options: list, defs: dict) -> LeafKind:
    """The most settable kind among a union's non-null members."""
    kinds = [_leaf_kind(option, defs) for option in options if option.get("type") != "null"]
    if "scalar" in kinds:
        return "scalar"
    if "mapping" in kinds:
        return "mapping"
    return "other"


def _plain_leaf_kind(prop: dict) -> LeafKind:
    if prop.get("type") == "array":
        return "other"
    if prop.get("type") == "object":
        if "additionalProperties" in prop:
            return "mapping"
        return "other"
    return "scalar"


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

    if prop.get("type") == "array":
        return _array_label(prop, defs)

    return prop.get("type", "any")


def _ref_type_label(ref: str, defs: dict) -> str:
    target = defs.get(ref, {})
    if "enum" in target:
        return _enum_label(target["enum"])
    if "properties" in target:
        return _entry_type_link(ref, defs)
    return ref


def _entry_type_link(ref: str, defs: dict) -> str:
    title = _entry_type_title(ref, defs)
    # The same slugify markdown's toc extension uses for heading ids, so the anchor cannot drift
    # from the heading it points at.
    return f"[{title}](#{slugify(title, '-')})"


def _entry_type_title(ref: str, defs: dict) -> str:
    return defs.get(ref, {}).get("title", ref)


def _ordered_entry_refs(rows: list[SettingRow], defs: dict) -> list[str]:
    """Models reachable from a row as a list entry, mapping value, or union member.

    Walks what it collects, so a model referenced only by another entry type still gets a table
    (and no type cell links at a heading that was never rendered).
    """
    refs: list[str] = []
    for row in rows:
        for ref in _model_refs(row.schema, defs):
            if ref not in refs:
                refs.append(ref)

    index = 0
    while index < len(refs):
        for prop in defs.get(refs[index], {}).get("properties", {}).values():
            for ref in _model_refs(prop, defs):
                if ref not in refs:
                    refs.append(ref)
        index += 1

    return refs


def _model_refs(prop: dict, defs: dict) -> list[str]:
    """Refs to models with fields, found anywhere in a property's schema."""
    ref = _extract_ref(prop)
    if ref is not None:
        if "properties" in defs.get(ref, {}):
            return [ref]
        return []

    refs = []
    nested = [*prop.get("anyOf", []), *prop.get("oneOf", [])]
    nested.extend(prop[key] for key in ("items", "additionalProperties") if isinstance(prop.get(key), dict))
    for option in nested:
        refs.extend(_model_refs(option, defs))
    return refs


def _build_entry_type(ref: str, defs: dict) -> EntryType:
    target = defs.get(ref, {})
    required = set(target.get("required", []))
    field_rows = [
        FieldRow(
            name=name,
            type_label=_resolve_type_label(prop, defs),
            default_label=_entry_default_label(prop, required=name in required),
            description=_normalize_cell(prop.get("description", "")),
        )
        for name, prop in target.get("properties", {}).items()
    ]
    return EntryType(
        title=_entry_type_title(ref, defs),
        description=_normalize_cell(_first_paragraph(target.get("description", ""))),
        field_rows=field_rows,
    )


def _entry_default_label(prop: dict, *, required: bool) -> str:
    if required:
        return "required"
    if "default" in prop:
        return f"`{_normalize_cell(json.dumps(prop['default']))}`"
    # A default_factory field carries no schema default; show the empty container it builds.
    if prop.get("type") == "array":
        return "`[]`"
    if prop.get("type") == "object":
        return "`{}`"
    return "-"


def _first_paragraph(text: str) -> str:
    """The model docstring's summary line, dropping the implementation notes below it."""
    return text.split("\n\n", maxsplit=1)[0]


def _enum_label(values: list) -> str:
    rendered = ", ".join(f"`{value}`" for value in values)
    return f"one of {rendered}"


def _any_of_label(any_of: list, defs: dict) -> str:
    options = [option for option in any_of if option.get("type") != "null"]
    if not options:
        return "any"
    # One member needs no grouping: there is nothing for its label to run together with.
    if len(options) == 1:
        return _resolve_type_label(options[0], defs)
    return " or ".join(_grouped_type_label(option, defs) for option in options)


def _grouped_type_label(prop: dict, defs: dict) -> str:
    """A type label parenthesized when it is composite, so nesting one in another stays readable.

    Without it, `list[str] | dict[str, str]` and `list[str | SomeModel]` both render as
    `array of string or object`, and only one of them means that.
    """
    label = _resolve_type_label(prop, defs)
    if _is_composite(prop, defs):
        return f"({label})"
    return label


def _is_composite(prop: dict, defs: dict) -> bool:
    """True when a property's label reads as more than one token: a union, array, enum, or const."""
    if "enum" in prop or "const" in prop:
        return True
    if prop.get("type") == "array":
        return True

    ref = _extract_ref(prop)
    if ref is not None:
        return "enum" in defs.get(ref, {})

    options = [option for option in prop.get("anyOf", []) if option.get("type") != "null"]
    if len(options) > 1:
        return True
    if len(options) == 1:
        return _is_composite(options[0], defs)
    return False


def _array_label(prop: dict, defs: dict) -> str:
    """Name the item type, so a list of models says what its entries are."""
    items = prop.get("items")
    if not isinstance(items, dict):
        return "array"
    return f"array of {_grouped_type_label(items, defs)}"


def _resolve_default_label(path: tuple[str, ...], defaults: dict) -> str:
    dotted = ".".join(path)
    if dotted in CWD_DEPENDENT_DEFAULTS:
        return f"`{CWD_DEPENDENT_DEFAULTS[dotted]}`"

    default_value = _lookup_default(path, defaults)

    if isinstance(default_value, list) and len(default_value) > MAX_DEFAULT_LIST_ITEMS:
        head = ", ".join(_normalize_cell(json.dumps(item)) for item in default_value[:MAX_DEFAULT_LIST_ITEMS])
        return f"`[{head}, ...]` ({len(default_value)} items)"

    return f"`{_normalize_cell(json.dumps(default_value))}`"


def _lookup_default(path: tuple[str, ...], defaults: dict) -> object:
    value: object = defaults
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _resolve_env_var_label(path: tuple[str, ...], prop: dict, defs: dict) -> str:
    env_var = "GTN_CONFIG_" + "__".join(key.upper() for key in path)
    kind = _leaf_kind(prop, defs)

    if kind == "mapping":
        return f"`{env_var}__<KEY>`"
    if kind == "other":
        return NOT_SETTABLE_FROM_ENV
    return f"`{env_var}`"


def _normalize_cell(text: str) -> str:
    """Flatten a value for a single markdown table cell."""
    collapsed = " ".join(text.split())
    return collapsed.replace("|", "\\|")


def _render_markdown(rows: list[SettingRow], entry_types: list[EntryType]) -> str:
    lines = [BANNER, "# Configuration Reference", ""]
    lines.append(
        "Every Griptape Nodes engine setting, grouped by category. Each setting can be placed in any "
        "`griptape_nodes_config.json` file (see [Engine Configuration](../guides/configuration.md) for the load "
        "order). A nested setting is listed under its full dotted key, the same form the config file and the "
        "`griptape-nodes config` commands use. Settings with a `GTN_CONFIG_*` env var, including the "
        "`GTN_CONFIG_<PATH>` form with `__` between the parts of a dotted key and the `GTN_CONFIG_<NAME>__<KEY>` "
        "form for a mapping-valued setting's entries, can also be overridden from the environment; list-valued "
        "settings must be edited in a config file. A mapping's keys are matched case-sensitively but the whole "
        "variable name is lowercased, so only an already-lowercase key is reachable from the environment (see the "
        "guide for details)."
    )
    lines.append("")

    for category in _ordered_categories(rows):
        category_rows = [row for row in rows if row.category.name == category.name]
        lines.append(f"## {category.name}")
        lines.append("")
        if category.description:
            lines.append(category.description)
            lines.append("")
        lines.append("| Setting | Type | Default | Environment variable | Description |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.extend(
            f"| `{row.name}` | {row.type_label} | {row.default_label} | {row.env_var_label} | {row.description} |"
            for row in category_rows
        )
        lines.append("")

    lines.extend(_render_entry_types(entry_types))

    return "\n".join(lines) + "\n"


def _render_entry_types(entry_types: list[EntryType]) -> list[str]:
    if not entry_types:
        return []

    lines = ["## Entry Types", ""]
    lines.append(
        "The fields of an object that appears inside a list- or mapping-valued setting. These belong "
        "to the individual entry, not to the setting, so they have no dotted key or env var of their own: "
        "edit them in the config file alongside the entry."
    )
    lines.append("")

    for entry_type in entry_types:
        lines.append(f"### {entry_type.title}")
        lines.append("")
        if entry_type.description:
            lines.append(entry_type.description)
            lines.append("")
        lines.append("| Field | Type | Default | Description |")
        lines.append("| --- | --- | --- | --- |")
        lines.extend(
            f"| `{field_row.name}` | {field_row.type_label} | {field_row.default_label} | {field_row.description} |"
            for field_row in entry_type.field_rows
        )
        lines.append("")

    return lines


def _ordered_categories(rows: list[SettingRow]) -> list[Category]:
    """Categories in first-appearance order (declaration order in the model)."""
    ordered: list[Category] = []
    seen_names = set()
    for row in rows:
        if row.category.name in seen_names:
            continue
        seen_names.add(row.category.name)
        ordered.append(row.category)
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
