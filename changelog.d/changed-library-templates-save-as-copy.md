- Saving a workflow template that came from a library now always writes a new copy in the workspace,
  leaving the library's file as the author shipped it. Before, only templates from Griptape's own
  libraries were protected this way; a template from any other library was overwritten in place. A
  workflow the user marked `is_template` themselves still saves normally.
