# Throwaway prototype. See the module docstring.
"""PROTOTYPE. Throwaway. The narrow version of "make the elements attrs".

Question: can BaseNodeElement be the attrs class while Parameter and friends keep their
hand-written __init__, and traits become attrs subclasses? If yes, the from-scratch design is
reachable without touching Parameter, the public API every node library calls.

Run: uv run python prototypes/attrs_element_base.py
"""

import uuid
from typing import Any, ClassVar

import attrs
from attrs import define, field


# Marks a field as element wiring rather than trait state. The base declares its own with
# this, so "fields are state" does not leak element_id and friends into a saved workflow.
WIRING: dict[str, bool] = {"wiring": True}


@define(eq=False, slots=False, kw_only=True)
class Element:
    """Stands in for BaseNodeElement, as attrs."""

    _stack: ClassVar[list["Element"]] = []

    element_id: str = field(factory=lambda: uuid.uuid4().hex, metadata=WIRING)
    element_type: str = field(default="Element", metadata=WIRING)
    name: str = field(factory=lambda: f"Element_{uuid.uuid4().hex}", metadata=WIRING)
    parent_group_name: str | None = field(default=None, metadata=WIRING)
    _children: list = field(factory=list, init=False)
    _parent: "Element | None" = field(default=None, init=False)
    _changes: dict = field(factory=dict, init=False)

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


class Parameter(Element):
    """Stands in for Parameter: keeps its hand-written __init__, unchanged in shape."""

    def __init__(
        self,
        name: str,
        tooltip: str,
        *,
        element_id: str | None = None,
        default_value: Any = None,
        traits: set | None = None,
    ) -> None:
        # The same call it makes today, into an attrs-generated base __init__.
        super().__init__(element_id=element_id if element_id is not None else uuid.uuid4().hex, name=name)
        self.tooltip = tooltip
        self.default_value = default_value
        for trait in traits or set():
            self.add_child(trait)


trait = define(eq=False, slots=False)


@trait
class Slider(Element):
    min: float = field(alias="min_val")
    max: float = field(alias="max_val")


@trait
class Button(Element):
    element_type: str = field(default="Button", metadata=WIRING)
    label: str = field(default="")
    on_click: Any = field(default=None, metadata={"behavior": True})


@trait
class Widget(Element):
    name: str = field(default="widget")
    library: str = field(default="")


def state_fields(cls: type) -> list[str]:
    """The saved contract: init fields that are neither element wiring nor behavior."""
    return [
        f.alias
        for f in attrs.fields(cls)
        if f.init and not f.metadata.get("wiring") and not f.metadata.get("behavior")
    ]


def show(label: str, value: Any) -> None:
    print(f"  {label:<52} {value}")


def main() -> None:
    print("\n1. An attrs base under a hand-written subclass")
    slider = Slider(min_val=0, max_val=1)
    parameter = Parameter("p", "t", traits={slider})
    show("Parameter's super().__init__ still works", (parameter.name, parameter.tooltip))
    show("its element_id was set", bool(parameter.element_id))
    show("the trait attached", [type(c).__name__ for c in parameter.children])

    print("\n2. An attrs subclass needs no post-init of its own")
    show("every field, wiring included", [f.name for f in attrs.fields(Slider)])
    show("the state contract", state_fields(Slider))
    show("Button: state versus behavior", (state_fields(Button), [f.name for f in attrs.fields(Button) if f.metadata.get("behavior")]))
    show("element_id defaulted by the base factory", bool(slider.element_id))
    show("two sliders differ", Slider(0, 1).element_id != Slider(0, 1).element_id)

    print("\n3. Adoption is inherited, not injected")
    with Parameter("group-ish", "t") as open_element:
        Slider(min_val=2, max_val=8)
        Button(label="Go")
    show("adopted into the open element", [type(c).__name__ for c in open_element.children])

    print("\n4. A subclass overriding an inherited field")
    show("Button's element_type default", Button().element_type)
    show("Slider keeps the base default", Slider(0, 1).element_type)
    show("Widget re-declares 'name', so it IS state", state_fields(Widget))
    show("and the value lands", Widget(name="w", library="l").name)
    show("wiring stays out of every other trait", (state_fields(Slider), state_fields(Button)))

    print("\n5. Identity, stated once by the decorator")
    a, b = Button(label="Go"), Button(label="Go")
    show("two identical Buttons: eq / hash / set", (a == b, hash(a) == hash(b), len({a, b})))
    show("(today's Button collapses to 1 in a set)", "eq=False makes every element identity")

    print("\n6. Ordering: does the base's post-init see subclass fields?")

    @trait
    class Noisy(Element):
        tag: str = field(default="tag")

        def __attrs_post_init__(self) -> None:
            print(f"    subclass post-init sees tag={self.tag!r}, element_id set={bool(self.element_id)}")
            super().__attrs_post_init__()

    with Parameter("outer", "t") as outer:
        Noisy(tag="hello")
    show("super().__attrs_post_init__() adopted it", [type(c).__name__ for c in outer.children])


if __name__ == "__main__":
    main()
