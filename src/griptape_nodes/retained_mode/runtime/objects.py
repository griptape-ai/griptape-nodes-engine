"""The named objects (flows and nodes) of one open workflow.

`WorkflowObjects` is the state a `WorkflowRuntime` keeps as `objects`. `ObjectManager` is the front that
resolves which workflow's objects the current work means.
"""

from __future__ import annotations

import re
from re import Pattern


class WorkflowObjects:
    """The flows and nodes of one open workflow, by name."""

    def __init__(self) -> None:
        self._name_to_objects: dict[str, object] = {}

    def __len__(self) -> int:
        return len(self._name_to_objects)

    def add(self, name: str, obj: object) -> None:
        if name in self._name_to_objects:
            msg = f"Attempted to add an object with name '{name}' but an object with that name already exists. The Object Manager is sacrosanct in this regard."
            raise ValueError(msg)
        self._name_to_objects[name] = obj

    def get(self, name: str) -> object | None:
        """The object named `name`, or None."""
        return self._name_to_objects.get(name)

    def has(self, name: str) -> bool:
        return name in self._name_to_objects

    def remove(self, name: str) -> object:
        """Forget the object named `name` and return it.

        Raises:
            KeyError: No object has that name.
        """
        return self._name_to_objects.pop(name)

    def rename(self, old_name: str, new_name: str) -> None:
        """Keep the object named `old_name` under `new_name` instead.

        Raises:
            KeyError: No object is named `old_name`.
        """
        self._name_to_objects[new_name] = self._name_to_objects.pop(old_name)

    def get_filtered_subset[T](
        self,
        name: str | Pattern | None = None,
        type: type[T] | None = None,  # noqa: A002
    ) -> dict[str, T]:
        """Filter the objects by name pattern and/or type.

        Args:
            name: A regex pattern string or compiled pattern to match names
            type: A type to match objects

        Returns:
            A new dictionary containing only the matching objects
        """
        result = {}

        # Compile pattern if it's a string
        if name and isinstance(name, str):
            name = re.compile(name)

        for key, value in self._name_to_objects.items():
            # Check key pattern if provided
            key_match = True
            if name:
                key_match = bool(name.search(key))

            # Check value type if provided
            value_match = True
            if type:
                value_match = isinstance(value, type)

            # Add to result if both conditions match
            if key_match and value_match:
                result[key] = value

        return result

    def get_as_type[T](self, name: str, cast_type: type[T]) -> T | None:
        """The object named `name` if there is one and it is a `cast_type`, else None."""
        obj = self._name_to_objects.get(name)
        if obj is not None and isinstance(obj, cast_type):
            return obj
        return None

    def generate_name(self, type_name: str, requested_name: str | None = None) -> str:
        """A name no object here has, built from the requested name or else the type name."""
        # Now ensure that we're giving a valid unique name. Here are the rules:
        # 1. If no name was requested, use the type name + first free integer.
        # 2. If a name was requested and no collision, use it as-is.
        # 3. If a name was requested and there IS a collision, check:
        #    a. If name ends in a number, find the FIRST prefix + integer value that isn't a collision.
        #    b. If name does NOT end in a number, use the name + first free integer.

        # We are going in with eyes open that the collision testing is inefficient.
        name_to_return = None
        incremental_prefix = ""

        if requested_name is None:
            # 1. If no name was requested, use the type name + first free integer.
            incremental_prefix = f"{type_name}_"
        elif requested_name not in self._name_to_objects:
            # 2. If a name was requested and no collision, use it as-is.
            name_to_return = requested_name
        else:
            # 3. If a name was requested and there IS a collision, check:
            pattern_match = re.search(r"\d+$", requested_name)
            if pattern_match is not None:
                #    a. If name ends in a number, find the FIRST prefix + integer value that isn't a collision.
                # Ends in a number. Find the FIRST prefix + integer value that isn't a collision.
                start = pattern_match.start()
                incremental_prefix = requested_name[:start]
            else:
                #    b. If name does NOT end in a number, use the name + first free integer.
                incremental_prefix = f"{requested_name}_"

        if name_to_return is None:
            # Do the incremental walk.
            curr_idx = 1
            done = False
            while not done:
                test_name = f"{incremental_prefix}{curr_idx}"
                if test_name not in self._name_to_objects:
                    # Found it.
                    name_to_return = test_name
                    done = True
                else:
                    # Keep going.
                    curr_idx += 1

        if name_to_return is None:
            msg = "Failed to generate a unique name for the object."
            raise ValueError(msg)

        return name_to_return
