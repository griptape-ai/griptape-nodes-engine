"""How the engine turns its objects into plain data and back.

``converter`` is complete only once the event modules have loaded: some payload types register
their hooks from their own modules, which import this package.
"""
