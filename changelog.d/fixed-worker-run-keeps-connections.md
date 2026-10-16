- Running a node whose library runs in a separate process no longer deletes connections when the
  node reacts to a value by changing its wires, such as `Load Pipeline Component` in the Modular
  Diffusion library dropping every wire it fed after a run.
