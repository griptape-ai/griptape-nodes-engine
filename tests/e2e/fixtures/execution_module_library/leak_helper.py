"""A helper that imports the execution module, making the violation transitive.

Imported the way a real library would: the engine puts the library's base directory on
sys.path, so its own subpackages import by plain name. Two node modules sharing a helper like
this is the case a scan of the node modules' own imports calls clean.
"""

from execution import runner  # noqa: F401
