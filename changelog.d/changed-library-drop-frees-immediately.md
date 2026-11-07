- `local_objects.drop` and `local_objects.drop_all` now free a library's object as soon as they
  are called, even while nodes are running, instead of waiting until no node is running. A library
  switching models now frees the old model before loading the new one, rather than holding both
  in GPU memory until the run ends.
