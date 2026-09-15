# Throwaway prototype. See the module docstring.
"""PROTOTYPE. Throwaway. Two questions:

  1. Do we need a decorator on every trait at all?
  2. What do the 30 element subclasses that hand-write __init__ look like under an attrs base?

Run: uv run python prototypes/attrs_no_decorator.py

Applies attrs from the element metaclass with auto_detect on, so a subclass either declares
fields and gets a constructor, or hand-writes a constructor and keeps it. No decorator anywhere.
"""

import uuid
from typing import Any, ClassVar, dataclass_transform

import attrs
from attrs import field

WIRING: dict[str, bool] = {"wiring": True}
BEHAVIOR: dict[str, bool] = {"behavior": True}


@dataclass_transform(field_specifiers=(field, attrs.Factory), eq_default=False)
class ElementMeta(type):
    """Applies attrs to every element subclass, so no class body carries a decorator.

    ``auto_detect`` is what lets one metaclass serve both kinds of subclass: a class that
    hand-writes ``__init__`` keeps it, and a class that only declares fields gets one generated.
    ``slots=False`` means attrs mutates the class in place rather than rebuilding it, so what
    this returns is the class Python just created.

    ``dataclass_transform`` is how a type checker learns a field declaration becomes a
    constructor argument. The same mechanism attrs puts on ``define`` itself.
    """

    def __new__(cls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any) -> type:
        created = super().__new__(cls, name, bases, namespace, **kwargs)
        return attrs.define(eq=False, slots=False)(created)


class Element(metaclass=ElementMeta):
    """Stands in for BaseNodeElement.

    Every wiring field is ``kw_only``, which is not decoration. attrs refuses a mandatory
    field declared after a defaulted one, and all of these are defaulted, so without
    ``kw_only`` no subclass could declare a mandatory field at all: ``Slider.min`` would have
    to invent a default. Keyword-only fields are moved to the end and exempted from that rule,
    which is what leaves a subclass free to declare either kind, positionally.
    """

    _stack: ClassVar[list["Element"]] = []

    element_id: str = field(factory=lambda: uuid.uuid4().hex, metadata=WIRING, kw_only=True)
    element_type: str = field(default="Element", metadata=WIRING, kw_only=True)
    name: str = field(factory=lambda: f"Element_{uuid.uuid4().hex}", metadata=WIRING, kw_only=True)
    _children: list = field(factory=list, init=False)
    _parent: "Element | None" = field(default=None, init=False)

    def __attrs_post_init__(self) -> None:
        current = Element._stack[-1] if Element._stack else None
        if current is not None:
            current.add_child(self)

    def __hash__(self) -> int:
        return hash(self.element_id)

    def add_child(self, child: "Element") -> None:
        self._children.append(child)
        child._parent = self

    @property
    def children(self) -> list:
        return self._children

    def __enter__(self) -> "Element":
        Element._stack.append(self)
        return self

    def __exit__(self, *exc: object) -> None:
        Element._stack.pop()


class Trait(Element):
    """Stands in for Trait. Declares no fields of its own, as today."""

    @classmethod
    def state_fields(cls) -> list[str]:
        return [
            f.alias for f in attrs.fields(cls) if f.init and not f.metadata.get("wiring") and not f.metadata.get("behavior")
        ]

    def to_state(self) -> dict[str, Any]:
        by_alias = {f.alias: f.name for f in attrs.fields(type(self))}
        return {alias: getattr(self, by_alias[alias]) for alias in self.state_fields()}


# ---------------------------------------------------------------------------------------
# Tier 1: declares fields, nothing else. Every simple trait.
# ---------------------------------------------------------------------------------------
class Slider(Trait):
    min: float = field(alias="min_val")
    max: float = field(alias="max_val")

    def ui_options_for_trait(self) -> dict:
        return {"slider": {"min_val": self.min, "max_val": self.max}}


class Button(Trait):
    element_type: str = field(default="Button", metadata=WIRING, kw_only=True)
    label: str = field(default="")
    button_link: str | None = field(default=None)
    on_click: Any = field(default=None, metadata=BEHAVIOR)


class Widget(Trait):
    name: str = field(default="widget")
    library: str = field(default="")


# ---------------------------------------------------------------------------------------
# Tier 2: declares fields and needs work at construction.
# ---------------------------------------------------------------------------------------
class AddParameterButton(Trait):
    element_type: str = field(default="AddParameterButton", metadata=WIRING, kw_only=True)

    def __attrs_post_init__(self) -> None:
        super().__attrs_post_init__()
        self.add_child(Button(label="AddParameter"))


class MultiOptions(Trait):
    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2"]

    _choices: list = field(factory=lambda: list(MultiOptions.DEFAULT_CHOICES))
    icon_size: str = field(default="small", converter=lambda v: v if v in ("small", "large") else "small")

    @property
    def choices(self) -> list:
        return self._choices


# ---------------------------------------------------------------------------------------
# Tier 3: hand-writes __init__, as Parameter and 29 other element classes do.
# auto_detect leaves it alone. Not one line changes.
# ---------------------------------------------------------------------------------------
class Parameter(Element):
    def __init__(
        self,
        name: str,
        tooltip: str,
        *,
        element_id: str | None = None,
        default_value: Any = None,
        traits: set | None = None,
        ui_options: dict | None = None,
    ) -> None:
        super().__init__(name=name, element_id=element_id if element_id is not None else uuid.uuid4().hex)
        self.tooltip = tooltip
        self.default_value = default_value
        self.ui_options = ui_options or {}
        for trait in traits or set():
            self.add_child(trait)

    def find_traits(self) -> list[Trait]:
        return [child for child in self._children if isinstance(child, Trait)]


class ParameterButton(Parameter):
    """A hand-written subclass of a hand-written subclass, building a trait for itself."""

    def __init__(self, name: str, tooltip: str, *, label: str = "", **kwargs: Any) -> None:
        super().__init__(name, tooltip, traits={Button(label=label)}, **kwargs)


# ---------------------------------------------------------------------------------------
# Tier 4: a trait subclassing a trait.
# ---------------------------------------------------------------------------------------
class Bounded(Trait):
    low: float = field(default=0.0)


class BoundedPair(Bounded):
    high: float = field(default=10.0)


def show(label: str, value: Any) -> None:
    print(f"  {label:<50} {value}")


def main() -> None:
    print("\nTier 1: fields only, no decorator, no post-init")
    slider = Slider(min_val=1, max_val=9)
    show("Slider(min_val=1, max_val=9).to_state()", slider.to_state())
    show("positional too", Slider(1, 9).to_state())
    show("element wiring set by the base", (bool(slider.element_id), slider.element_type))
    show("wiring stays out of state", slider.state_fields())
    show("Button state vs behavior", (Button().state_fields(), Button(on_click=print).on_click.__name__))
    show("Widget re-declares name, so it is state", Widget(name="w", library="l").to_state())
    show("hashable and set-safe", len({Slider(0, 1), Slider(0, 1)}))

    print("\nTier 2: fields plus construction work")
    add_button = AddParameterButton()
    show("child attached after super() post-init", [type(c).__name__ for c in add_button.children])
    multi = MultiOptions(icon_size="huge")
    show("converter snapped icon_size", multi.icon_size)
    multi.icon_size = "enormous"
    show("a later write is converted too", multi.icon_size)
    show("fresh default list per instance", MultiOptions().choices is not MultiOptions().choices)

    print("\nTier 3: hand-written __init__, untouched by the metaclass")
    parameter = Parameter("p", "t", traits={Slider(min_val=0, max_val=1)})
    show("its own signature still positional", (parameter.name, parameter.tooltip))
    show("attrs left the constructor alone", "__init__" in Parameter.__dict__)
    show("base fields still initialized", bool(parameter.element_id))
    show("traits attached", [type(t).__name__ for t in parameter.find_traits()])
    show("non-field attribute assigned freely", parameter.ui_options)
    nested = ParameterButton("b", "t", label="Go")
    show("subclass of a hand-written subclass", [type(t).__name__ for t in nested.find_traits()])

    print("\nTier 4: a trait subclassing a trait")
    pair = BoundedPair(low=1, high=2)
    show("fields collected from both", pair.state_fields())
    show("state", pair.to_state())

    print("\nContext adoption, inherited by every tier")
    with Parameter("open", "t") as open_parameter:
        Slider(min_val=0, max_val=1)
        AddParameterButton()
    show("adopted", [type(c).__name__ for c in open_parameter.children])
    show("the nested Button went to its own parent", [type(c).__name__ for c in open_parameter.children[1].children])


if __name__ == "__main__":
    main()
