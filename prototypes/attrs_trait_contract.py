# Throwaway prototype. See the module docstring.
"""PROTOTYPE. Throwaway. Answers one question: can attrs field declarations be the trait
serialization contract, replacing the STATE/CALLBACKS dicts, without losing anything?

Run: uv run python prototypes/attrs_trait_contract.py

VERDICT: yes, and it is strictly stronger than the dicts. Every hard case cleared:

  - `field(alias="min_val")` carries Slider's keyword/attribute mismatch, positionally too.
  - a private field under a public property (`_choices` / `choices`) is the attrs idiom, so
    the field-versus-property clash that blocked dataclasses is a non-issue.
  - `_init_element()` (added to BaseNodeElement) lets a generated constructor set up the
    element, and leaving already-set attributes alone is what lets Widget declare `name`.
  - subclass field collection is automatic, so "spread the base's STATE in" goes away.
  - field converters make apply_state interpret state exactly as constructing does.
  - `__attrs_init_subclass__` checks the contract when the class is built, so two of the
    three save-time warnings become impossible or become import-time errors.
  - `attrs.resolve_types` recovers real types under PEP 563; only a forward reference to a
    later-defined class escapes, and its values are still checked at save time.
  - a cross-field invariant (Button's link versus handler) needs a class-level `on_setattr`;
    a per-field validator does not fire for a write to the *other* field.
  - element identity, hashing, set membership, context adoption, to_dict, deepcopy, pickle,
    and attaching to a Parameter all behave.

The cost is not technical: every existing trait is rewritten, and library authors get a
second migration note one release after being told to hand-write `__init__`.

Deliberately no `from __future__ import annotations`, so attrs hands back real type objects
rather than strings. The PEP 563 case is probed at the end.

Converts only the hard traits. If Button, Options, MultiOptions, Widget, AddParameterButton
and Slider survive, the easy ten do too.
"""

import json
import logging
import pickle
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, UnionType
from typing import Any, ClassVar, Literal, Union, get_args, get_origin

import attrs
from collections.abc import Callable
from collections.abc import Callable as CallableABC

from attrs import define, field

from griptape_nodes.exe_types.callback_binding import name_callback, resolve_callback
from griptape_nodes.exe_types.core_types import BaseNodeElement, Parameter, ParameterGroup, Trait
from griptape_nodes.exe_types.trait_state import as_saved_state_value

logging.basicConfig(level=logging.WARNING, format="  WARNING: %(message)s")

# A field holding behavior rather than state. Carried by method name, never saved as data.
BEHAVIOR: dict[str, bool] = {"behavior": True}

# What a state field's declared type may be. A saved workflow is data.
SAVEABLE_TYPES = (type(None), bool, int, float, str, list, dict, set, frozenset, tuple)


def unsaveable_type(annotation: Any) -> str | None:
    """Name the part of an annotation that no saved workflow could hold, or None.

    Walks unions and containers, because a trait field is almost always ``X | None`` or
    ``list[X]``. ``Any`` is unanswerable and passes; its values are checked at save time like
    everything else.
    """
    if annotation is Any or annotation is None:
        return None
    origin = get_origin(annotation)
    if origin is Literal:
        # The args are values, not types. A literal of saveable scalars is saveable.
        for value in get_args(annotation):
            if not isinstance(value, (type(None), bool, int, float, str)):
                return type(value).__name__
        return None
    if origin in (Union, UnionType):
        for member in get_args(annotation):
            found = unsaveable_type(member)
            if found is not None:
                return found
        return None
    if origin is not None:
        if origin is CallableABC:
            return "callback"
        found = unsaveable_type(origin)
        if found is not None:
            return found
        for member in get_args(annotation):
            if member is Ellipsis:
                continue
            found = unsaveable_type(member)
            if found is not None:
                return found
        return None
    if isinstance(annotation, str):
        # PEP 563 left the annotation unresolved. Nothing to check here; save time still will.
        return None
    if isinstance(annotation, type) and issubclass(annotation, CallableABC):
        return "callback"
    if isinstance(annotation, type) and not issubclass(annotation, SAVEABLE_TYPES):
        return annotation.__name__
    if callable(annotation) and not isinstance(annotation, type):
        return "callback"
    return None


class ContractError(TypeError):
    """A trait whose declarations could never round-trip. Raised at class creation."""


class AttrsTrait(Trait):
    """Trait whose state contract is its attrs fields.

    The declaration is the field list, so there is no second place to keep in step. A field's
    ``alias`` is the constructor keyword a save records; its ``name`` is where the value
    lives, which is how ``Slider(min_val=...)`` can store ``self.min`` and how a field can sit
    behind a property (``_choices`` under ``choices``).
    """

    @classmethod
    def __attrs_init_subclass__(cls) -> None:
        """Check the contract when the class is built, not when an artist saves.

        attrs runs this on the nearest non-attrs ancestor after the fields are collected, so
        this is the first moment the whole declaration exists.
        """
        try:
            attrs.resolve_types(cls)
        except NameError:
            # A forward reference to something defined later in the module. Unresolvable now,
            # and the walker passes a string through, so its values are checked at save time.
            pass
        for attribute in attrs.fields(cls):
            if attribute.metadata.get("behavior"):
                continue
            if not attribute.init:
                msg = (
                    f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but its constructor "
                    f"takes no argument for it, so a load could never restore it. Pass init=True, or mark "
                    f"it behavior."
                )
                raise ContractError(msg)
            declared_type = attribute.type
            unsaveable = unsaveable_type(declared_type)
            if unsaveable == "callback":
                msg = (
                    f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but its type is a callback. "
                    f"Mark it behavior with metadata=BEHAVIOR so it is carried by method name."
                )
                raise ContractError(msg)
            if unsaveable is not None:
                msg = (
                    f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but a {unsaveable} "
                    f"cannot be written to a saved workflow. Trait state holds text, numbers, true/false, and "
                    f"lists or dictionaries of those."
                )
                raise ContractError(msg)

    @classmethod
    def state_declarations(cls) -> tuple[attrs.Attribute, ...]:
        if not attrs.has(cls):
            return ()
        return tuple(f for f in attrs.fields(cls) if not f.metadata.get("behavior"))

    @classmethod
    def behavior_declarations(cls) -> tuple[attrs.Attribute, ...]:
        if not attrs.has(cls):
            return ()
        return tuple(f for f in attrs.fields(cls) if f.metadata.get("behavior"))

    def to_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for attribute in self.state_declarations():
            saved = as_saved_state_value(getattr(self, attribute.name))
            if saved.unsupported_type is not None:
                # Reachable only for a container whose *items* are unsaveable, since the
                # declared type was checked at class creation.
                logger.warning(
                    "Trait '%s' holds a %s in '%s', which has no saved form. The parameter will load without it.",
                    type(self).__name__,
                    saved.unsupported_type,
                    attribute.alias,
                )
                continue
            state[attribute.alias] = saved.value
        return state

    def apply_state(self, state: dict[str, Any]) -> None:
        """Overwrite state in place, keeping the instance the node's __init__ built.

        Built as a throwaway first so a state the constructor rejects leaves this trait
        untouched. The per-field converters then run again on assignment, so writing is
        interpreted exactly as constructing is.
        """
        if not state:
            return
        interpreted = type(self).from_state(state)
        for attribute in self.state_declarations():
            if attribute.alias in state:
                setattr(self, attribute.name, getattr(interpreted, attribute.name))

    def callback_names(self, owner: Any) -> dict[str, str]:
        names: dict[str, str] = {}
        for attribute in self.behavior_declarations():
            name = name_callback(getattr(self, attribute.name, None), owner)
            if name is not None:
                names[attribute.alias] = name
        return names

    def unnameable_callbacks(self, owner: Any) -> list[str]:
        unnameable: list[str] = []
        for attribute in self.behavior_declarations():
            callback = getattr(self, attribute.name, None)
            if callback is not None and name_callback(callback, owner) is None:
                unnameable.append(attribute.alias)
        return sorted(unnameable)

    def apply_callback_names(self, names: dict[str, str], owner: Any) -> None:
        by_alias = {f.alias: f for f in self.behavior_declarations()}
        for alias, method_name in names.items():
            attribute = by_alias.get(alias)
            if attribute is None or getattr(self, attribute.name, None) is not None:
                continue
            callback = resolve_callback(method_name, owner, described_as=f"the '{alias}' behavior")
            if callback is not None:
                setattr(self, attribute.name, callback)


logger = logging.getLogger("griptape_nodes")


# --------------------------------------------------------------------------------------
# 1. Slider: constructor keyword differs from the attribute (the old STATE_ALIASES case)
# --------------------------------------------------------------------------------------
@define(eq=False, slots=False)
class Slider(AttrsTrait):
    min: float = field(alias="min_val")
    max: float = field(alias="max_val")

    def __attrs_post_init__(self) -> None:
        self._init_element()

    def ui_options_for_trait(self) -> dict:
        return {"slider": {"min_val": self.min, "max_val": self.max}}


# --------------------------------------------------------------------------------------
# 2. Widget: declares a field named `name`, which the element base also sets
# --------------------------------------------------------------------------------------
@define(eq=False, slots=False)
class Widget(AttrsTrait):
    name: str = field()
    library: str = field()

    def __attrs_post_init__(self) -> None:
        self._init_element()


# --------------------------------------------------------------------------------------
# 3. AddParameterButton: no state, and its constructor attaches a child element
# --------------------------------------------------------------------------------------
@define(eq=False, slots=False)
class AddParameterButton(AttrsTrait):
    def __attrs_post_init__(self) -> None:
        self._init_element(element_id="AddParameterButton")
        self.type = "AddParameter"
        self.add_child(BaseNodeElement(name="pretend-button"))


# --------------------------------------------------------------------------------------
# 4. Options: a private field behind a public property, and a fresh default list
# --------------------------------------------------------------------------------------
@define(eq=False, slots=False)
class Options(AttrsTrait):
    DEFAULT_CHOICES: ClassVar[list[str]] = ["choice 1", "choice 2", "choice 3"]

    # Field is _choices; attrs strips the underscore for the keyword, so the saved key and
    # the constructor argument are both "choices", and the property below is unobstructed.
    _choices: list = field(factory=lambda: list(Options.DEFAULT_CHOICES))
    show_search: bool = field(default=True)
    allow_custom: bool = field(default=False)

    def __attrs_post_init__(self) -> None:
        self._init_element()

    @property
    def choices(self) -> list:
        return self._choices

    @choices.setter
    def choices(self, value: list) -> None:
        self._choices = value


# --------------------------------------------------------------------------------------
# 5. MultiOptions: a constructor that coerces, which apply_state has to reproduce
# --------------------------------------------------------------------------------------
def _snap_icon_size(value: str) -> str:
    if value not in ("small", "large"):
        return "small"
    return value


@define(eq=False, slots=False)
class MultiOptions(AttrsTrait):
    _choices: list = field(factory=list)
    placeholder: str = field(default="Select options...")
    icon_size: str = field(default="small", converter=_snap_icon_size)

    def __attrs_post_init__(self) -> None:
        self._init_element()

    @property
    def choices(self) -> list:
        return self._choices


# --------------------------------------------------------------------------------------
# 6. Button: state and behavior side by side, plus an invariant between two fields
# --------------------------------------------------------------------------------------
@define(eq=False, slots=False)
class Button(AttrsTrait):
    label: str = field(default="")
    variant: str = field(default="secondary")
    button_link: str | None = field(default=None)
    # The nine remaining styling fields are the same shape as label; omitted for the spike.
    on_click: Any = field(default=None, metadata=BEHAVIOR)
    get_button_state: Any = field(default=None, metadata=BEHAVIOR)

    def __attrs_post_init__(self) -> None:
        self._init_element(element_id="Button")

    @button_link.validator
    def _one_click_action(self, attribute: attrs.Attribute, value: str | None) -> None:
        if value is not None and self.on_click is not None:
            msg = "Cannot specify both 'button_link' and 'on_click' for Button."
            raise ValueError(msg)

    @property
    def on_click_callback(self) -> Any:
        """What a click fires, whether the node supplied it or the link derived it."""
        if self.button_link is not None:
            return lambda *args: f"open {self.button_link}"
        return self.on_click


# --------------------------------------------------------------------------------------
# 7. Compare: a hand-written trait, undecorated, as a third-party one still would be
# --------------------------------------------------------------------------------------
class Compare(AttrsTrait):
    def converters_for_trait(self) -> list:
        return []


# --------------------------------------------------------------------------------------
# 8. Inheritance: a subclass contributes a field, and the base's come along
# --------------------------------------------------------------------------------------
@define(eq=False, slots=False)
class Bounded(AttrsTrait):
    low: float = field(default=0.0)

    def __attrs_post_init__(self) -> None:
        self._init_element()


@define(eq=False, slots=False)
class BoundedPair(Bounded):
    high: float = field(default=10.0)


# --------------------------------------------------------------------------------------
# 9. Button again, with the cross-field invariant enforced on every write, not just at
#    construction. A per-field validator only fires for the field being written, so
#    `button.on_click = handler` slips past button_link's validator.
# --------------------------------------------------------------------------------------
def _one_action_only(instance: "GuardedButton", attribute: attrs.Attribute, value: Any) -> Any:
    """Reject a write that would leave a button with both a link and a handler."""
    other = "on_click" if attribute.name == "button_link" else "button_link"
    if value is not None and getattr(instance, other, None) is not None:
        msg = f"Cannot set '{attribute.name}': the button already has '{other}'."
        raise ValueError(msg)
    return value


@define(eq=False, slots=False, on_setattr=[attrs.setters.convert, attrs.setters.validate, _one_action_only])
class GuardedButton(AttrsTrait):
    label: str = field(default="")
    button_link: str | None = field(default=None)
    on_click: Any = field(default=None, metadata=BEHAVIOR)

    def __attrs_post_init__(self) -> None:
        self._init_element(element_id="GuardedButton")


def _declare(name: str, annotation: Any, *, init: bool = True) -> type:
    """Build a one-field attrs trait, the way a library author's class body would."""
    namespace = {"__annotations__": {name: annotation}, name: field(default=None, init=init)}
    return define(eq=False, slots=False)(type("Probe", (AttrsTrait,), namespace))


def trait(maybe_cls: type | None = None, *, element_id: str | None = None) -> Any:
    """Declare a trait. One decorator, so an author remembers no flags.

    Bakes in what a trait always needs: no generated ``__eq__`` (the base compares by element
    identity, and traits live in sets), a ``__dict__`` so a trait can hold a derived attribute
    that is not state, converters and validators on every write, and the element wiring.

    Element setup runs before anything an author asked for, so ``on_trait_created`` can attach
    children or read ``self.element_id``.
    """

    def wrap(cls: type) -> type:
        author_hook = getattr(cls, "on_trait_created", None)

        def __attrs_post_init__(self: Any) -> None:  # noqa: N807
            self._init_element(element_id=element_id)
            if author_hook is not None:
                author_hook(self)

        cls.__attrs_post_init__ = __attrs_post_init__  # type: ignore[attr-defined]
        return define(eq=False, slots=False, on_setattr=[attrs.setters.convert, attrs.setters.validate])(cls)

    if maybe_cls is None:
        return wrap
    return wrap(maybe_cls)


# --------------------------------------------------------------------------------------
# 12. The authoring surface: the same traits, written the way a library author would
# --------------------------------------------------------------------------------------
@trait
class AuthoredSlider(AttrsTrait):
    min: float = field(alias="min_val")
    max: float = field(alias="max_val")

    def ui_options_for_trait(self) -> dict:
        return {"slider": {"min_val": self.min, "max_val": self.max}}


@trait(element_id="AddParameterButton")
class AuthoredAddParameterButton(AttrsTrait):
    def on_trait_created(self) -> None:
        self.type = "AddParameter"
        self.add_child(BaseNodeElement(name="pretend-button"))


@trait
class AuthoredWidget(AttrsTrait):
    name: str = field()
    library: str = field()


def report(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(label: str, value: Any) -> None:
    print(f"  {label:<44} {value}")


def main() -> None:  # noqa: C901, PLR0915
    report("1. Declaration: what a trait author writes")
    for cls in (Slider, Options, Button, BoundedPair):
        state = [f"{f.alias} -> self.{f.name}" for f in cls.state_declarations()]
        behavior = [f.alias for f in cls.behavior_declarations()]
        print(f"  {cls.__name__}:")
        show("state", state)
        show("behavior", behavior)

    report("2. Slider: keyword differs from attribute, positionally and by keyword")
    show("Slider(1, 9).min/.max", (Slider(1, 9).min, Slider(1, 9).max))
    slider = Slider(min_val=1, max_val=9)
    show("to_state()", slider.to_state())
    show("json round trip", Slider.from_state(json.loads(json.dumps(slider.to_state()))).to_state())
    show("hasattr(trait, 'min') duck-typing still works", hasattr(slider, "min"))

    report("3. Element wiring survives a generated constructor")
    show("element_id set", bool(slider.element_id))
    show("element_type set", slider.element_type)
    show("name defaulted", slider.name.startswith("BaseNodeElement_"))
    show("hash == hash(element_id)", hash(slider) == hash(slider.element_id))
    show("two traits in a set", len({Slider(0, 1), Slider(0, 1)}))
    show("engine internals absent from state", not ({"_children", "_parent", "element_id"} & slider.to_state().keys()))

    widget = Widget(name="my-widget", library="lib")
    show("Widget's own 'name' field survives base init", widget.name)
    show("Widget state", widget.to_state())

    with ParameterGroup(name="group") as group:
        Slider(min_val=0, max_val=1)
    show("adopted by the open element context", [type(child).__name__ for child in group.children])

    button = AddParameterButton()
    show("AddParameterButton child attached", [child.name for child in button.children])
    show("its fixed element_id", button.element_id)

    report("4. A private field behind a public property")
    options = Options()
    show("default choices (fresh list per instance)", options.choices)
    show("independent defaults", Options().choices is not Options().choices)
    show("to_state key is the public keyword", options.to_state())
    show("constructor takes the public keyword", Options(choices=["a"]).choices)
    options.choices = ["x"]
    show("property write lands on the field", options.to_state()["choices"])

    report("5. apply_state reproduces what the constructor would do")
    multi = MultiOptions(choices=["a"], placeholder="Pick one")
    multi.apply_state({"choices": ["b"], "icon_size": "huge"})
    show("icon_size coerced by the field converter", multi.icon_size)
    show("matches a fresh build", MultiOptions.from_state({"icon_size": "huge"}).icon_size)
    show("a key the state omits keeps what __init__ built", multi.placeholder)
    show("a key it carries is written", multi.choices)

    slider = Slider(min_val=0, max_val=1)
    try:
        slider.apply_state({"min_val": 2})  # max_val is required
    except TypeError as error:
        show("unsatisfiable state raises", type(error).__name__)
    show("and leaves the trait untouched", (slider.min, slider.max))

    report("6. Button: state, behavior, and an invariant between them")

    class FakeNode:
        def handle_click(self, *args: Any) -> None:
            return None

    node = FakeNode()
    linked = Button(label="Docs", button_link="https://example.com")
    show("link button state", linked.to_state())
    show("its callbacks", linked.callback_names(node))
    show("derived handler works", linked.on_click_callback())

    handled = Button(label="Run", on_click=node.handle_click)
    show("handler button state", handled.to_state())
    show("its callbacks", handled.callback_names(node))
    show("no link recorded", handled.to_state()["button_link"])

    rebuilt = Button.from_state(handled.to_state())
    rebuilt.apply_callback_names(handled.callback_names(node), node)
    show("callback rebound on load", rebuilt.on_click == node.handle_click)

    lambda_button = Button(label="x", on_click=lambda: None)
    show("unnameable callback reported", lambda_button.unnameable_callbacks(node))

    try:
        Button(button_link="https://example.com", on_click=node.handle_click)
    except ValueError as error:
        show("both-at-once rejected at construction", str(error)[:48])
    try:
        linked.on_click = node.handle_click
        show("drift after construction NOT caught", (linked.button_link, bool(linked.on_click)))
    except ValueError as error:
        show("drift after construction rejected", type(error).__name__)

    report("7. Traits that declare nothing, and inheritance")
    show("undecorated Compare has no state", (attrs.has(Compare), Compare().to_state()))
    pair = BoundedPair(low=1, high=2)
    show("subclass collects its base's fields", [f.alias for f in BoundedPair.state_declarations()])
    show("both round trip", BoundedPair.from_state(pair.to_state()).to_state())

    report("8. The contract is checked when the class is built")
    for label, declare in (
        ("a bare unsaveable type", lambda: _declare("root", Path)),
        ("unsaveable inside a union", lambda: _declare("root", Path | None)),
        ("unsaveable inside a list", lambda: _declare("roots", list[Path])),
        ("unsaveable as a dict value", lambda: _declare("roots", dict[str, Path])),
        ("a callback not marked behavior", lambda: _declare("on_ping", Callable | None)),
        ("a field the constructor cannot take", lambda: _declare("derived", str, init=False)),
    ):
        try:
            declare()
            show(label, "PASSED, not caught")
        except ContractError as error:
            show(label, f"ContractError: {str(error).split('. ')[0]}")

    for label, declare in (
        ("a saveable union", lambda: _declare("tip", str | None)),
        ("a saveable container", lambda: _declare("names", list[str])),
        ("a Literal of strings", lambda: _declare("format", Literal["hex", "rgb"])),
        ("Any, which cannot be judged", lambda: _declare("value", Any)),
    ):
        try:
            declare()
            show(label, "accepted")
        except ContractError as error:
            show(label, f"WRONGLY REJECTED: {error}")

    report("9. Closing the drift hole with a class-level on_setattr")
    guarded = GuardedButton(label="Docs", button_link="https://example.com")
    try:
        guarded.on_click = node.handle_click
        show("drift after construction NOT caught", (guarded.button_link, bool(guarded.on_click)))
    except ValueError as error:
        show("drift after construction rejected", str(error))
    show("and the write did not land", (guarded.button_link, guarded.on_click))
    guarded.button_link = None
    guarded.on_click = node.handle_click
    show("clearing the link first is allowed", (guarded.button_link, guarded.on_click.__name__))

    report("10. The rest of the element contract")
    slider = Slider(min_val=0, max_val=1)
    show("to_dict() reports what the editor reads", sorted(slider.to_dict())[:6])
    show("trait_name in to_dict", slider.to_dict()["trait_name"])
    copied = deepcopy(slider)
    show("deepcopy keeps state", (copied.min, copied.max, copied is not slider))
    show("pickle round trip", pickle.loads(pickle.dumps(slider)).to_state())
    parameter = Parameter(name="p", tooltip="t", traits={Slider(min_val=0, max_val=1)})
    show("attaches to a Parameter", [type(t).__name__ for t in parameter.find_elements_by_type(Trait)])

    report("11. What still leaks through, and the PEP 563 question")
    saved = Button(label="x")
    saved.label = {"a": Path("/tmp")}  # type: ignore[assignment]
    show("unsaveable *item* still needs a save-time warning", saved.to_state())

    module = ModuleType("pep563_probe")
    module.__dict__.update({"AttrsTrait": AttrsTrait, "define": define, "field": field})
    sys.modules[module.__name__] = module
    source = (
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "@define(eq=False, slots=False)\n"
        "class Deferred(AttrsTrait):\n"
        "    root: Path | None = field(default=None)\n"
    )
    try:
        exec(compile(source, module.__name__, "exec"), module.__dict__)  # noqa: S102
        show("PEP 563 module: string annotation", "PASSED, not caught")
    except ContractError as error:
        show("PEP 563 module: resolve_types caught it", str(error).split(". ")[0])

    forward = (
        "from __future__ import annotations\n"
        "@define(eq=False, slots=False)\n"
        "class Forward(AttrsTrait):\n"
        "    later: LaterDefined | None = field(default=None)\n"
        "class LaterDefined: pass\n"
    )
    try:
        exec(compile(forward, module.__name__, "exec"), module.__dict__)  # noqa: S102
        show("forward reference to a later class", "passed unchecked (as designed)")
    except ContractError as error:
        show("forward reference to a later class", f"ContractError: {str(error).split('. ')[0]}")
    except NameError as error:
        show("forward reference to a later class", f"NameError leaked: {error}")

    report("12. The authoring surface")
    authored = AuthoredSlider(min_val=2, max_val=8)
    show("no post-init, no flags, still an element", (bool(authored.element_id), authored.to_state()))
    show("still hashable and set-safe", len({AuthoredSlider(0, 1), AuthoredSlider(0, 1)}))
    show("child attached by the author hook", [c.name for c in AuthoredAddParameterButton().children])
    show("fixed element_id from the decorator", AuthoredAddParameterButton().element_id)
    show("a field named 'name' still wins", AuthoredWidget(name="w", library="l").to_state())
    with ParameterGroup(name="g") as authored_group:
        AuthoredSlider(0, 1)
    show("adopted by the open context", [type(c).__name__ for c in authored_group.children])


if __name__ == "__main__":
    main()
