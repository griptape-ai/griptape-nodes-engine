- Saving a workflow that came from any library, template or not, now writes a new copy in the
  workspace, leaving the library's file as the author shipped it. Before, only workflows from
  Griptape's own libraries were protected this way; one from any other library was overwritten in
  place. A workflow the user marked `is_template` themselves still saves normally.
