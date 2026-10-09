"""How the engine turns its objects into plain data and back.

``hooks.configure_converter`` holds the hooks every converter shares. The converters themselves,
one per reader of a message, are ``retained_mode.events.converters.engine`` and ``client``.
"""
