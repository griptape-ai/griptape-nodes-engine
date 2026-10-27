- Creating a global variable with `CreateVariableRequest` now fails when no workflow is open,
  instead of storing it for the whole engine. Node library code that calls
  `ObjectManager.add_object_by_name` or `ObjectManager.del_obj_by_name` also needs an open
  workflow, and raises an error without one.
