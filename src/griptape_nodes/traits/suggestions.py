from dataclasses import dataclass, field
from typing import Any

from griptape_nodes.exe_types.core_types import Trait


@dataclass
class Suggestion:
    """One row offered by a :class:`Suggestions` list.

    ``name`` is the text filled into the field when the row is picked, so it is the
    value the node receives. ``label`` is display-only: use it when the stored value
    is an id a person would not recognize. ``subtitle`` and ``icon`` decorate the row
    in the list and are never stored.
    """

    name: str
    label: str | None = None
    subtitle: str | None = None
    icon: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"name": self.name}
        if self.label is not None:
            row["label"] = self.label
        if self.subtitle is not None:
            row["subtitle"] = self.subtitle
        if self.icon is not None:
            row["icon"] = self.icon
        return row


@dataclass(eq=False)
class Suggestions(Trait):
    """Turns a text parameter into a typeahead: the user types, the editor suggests.

    This is the free-text counterpart to ``Options``, and the two are different
    widgets rather than two settings of one widget:

    - ``Options`` is a dropdown. The value must be one of ``choices``; anything else
      is rejected. Reach for it when an unlisted value would fail at run time.
    - ``Options(allow_user_created_options=True)`` is still a dropdown, but tolerates
      a value the user typed. Reach for it when picking is the normal path and typing
      is the exception.
    - ``Suggestions`` is a plain text field that offers matching rows once the user
      starts typing. The value is always whatever they typed. Reach for it when the
      list is a convenience and the full set of valid values is open-ended.

    Because the value is never constrained, this trait adds no converter and no
    validator. It is presentation only, which is why ``choices`` can be updated at
    run time without any risk of invalidating a value the node already holds.

    Choices are plain strings, or ``Suggestion`` rows when a row needs a friendly
    label, a subtitle, or an icon:

        Parameter(
            name="model",
            type="str",
            tooltip="Pick a suggested model or type your own id",
            traits={
                Suggestions(
                    choices=[
                        Suggestion("gpt-4.1", label="GPT-4.1", subtitle="OpenAI"),
                        "claude-sonnet-4-5",
                    ]
                )
            },
        )

    Do not combine this with ``Options`` on the same parameter. Both write the key
    the editor keys off, the dropdown wins, and the suggestions are silently ignored.
    """

    _choices: list[Suggestion] = field(default_factory=list)
    element_id: str = field(default_factory=lambda: "Suggestions")

    def __init__(self, *, choices: list[str | Suggestion] | None = None) -> None:
        super().__init__()
        if choices is None:
            self.choices = []
        else:
            self.choices = choices

    @property
    def choices(self) -> list[Suggestion]:
        """The rows offered as the user types.

        Unlike ``Options.choices``, this reads the trait's own field rather than
        ui_options. Nothing in the engine consults these rows -- there is no converter
        or validator to go stale against a saved workflow -- so the round trip through
        ui_options that ``Options`` needs would buy nothing here.
        """
        return self._choices

    @choices.setter
    def choices(self, value: list[str | Suggestion]) -> None:
        """Replace the rows, publishing them to the editor.

        Writing to ui_options is what makes a run-time update visible, and what makes
        it survive a save/reload, since ui_options is serialized and trait fields are not.
        """
        rows = [self._coerce_row(index, entry) for index, entry in enumerate(value)]

        self._choices = rows

        if self._parent and hasattr(self._parent, "ui_options"):
            ui_options = getattr(self._parent, "ui_options", None)
            if not isinstance(ui_options, dict):
                self._parent.ui_options = {}  # type: ignore[attr-defined]
            self._parent.ui_options["suggestions"] = [row.to_dict() for row in rows]  # type: ignore[attr-defined]

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["suggestions"]

    def ui_options_for_trait(self) -> dict:
        return {"suggestions": [row.to_dict() for row in self._choices]}

    @staticmethod
    def _coerce_row(index: int, entry: str | Suggestion) -> Suggestion:
        if isinstance(entry, Suggestion):
            if not entry.name.strip():
                msg = (
                    f"Attempted to build a suggestion list. Failed because suggestion {index + 1} has a blank name. "
                    f"A suggestion's name is the text filled into the field when it is picked, so it cannot be empty."
                )
                raise ValueError(msg)
            return entry

        if isinstance(entry, str):
            if not entry.strip():
                msg = (
                    f"Attempted to build a suggestion list. Failed because suggestion {index + 1} is blank. "
                    f"Every suggestion needs text to offer the user."
                )
                raise ValueError(msg)
            return Suggestion(name=entry)

        msg = (
            f"Attempted to build a suggestion list. Failed because suggestion {index + 1} is a "
            f"{type(entry).__name__}. Suggestions must be text, or a Suggestion when the row needs a "
            f"label, subtitle, or icon."
        )
        raise TypeError(msg)
