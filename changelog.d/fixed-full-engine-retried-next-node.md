- A library whose nodes run on another engine that had no room for its worker now asks that engine
  again on its next node. Before, every later node failed with the same "no room" reason until the
  library was reloaded, even after the other engine had space.
