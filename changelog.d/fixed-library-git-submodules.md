- Libraries that vendor code as git submodules (such as SAM3 and MiniMax Remover) now get their
  submodules on install, update, and branch or tag switch. Updating them no longer stops with an
  "Uncommitted Changes" prompt that "Overwrite" could not clear. A SAM3 install that already shows
  the prompt needs one "Overwrite".
  [#5784](https://github.com/griptape-ai/griptape-nodes-engine/issues/5784)
