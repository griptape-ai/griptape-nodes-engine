"""Test support shared by the engine's tests and the tests of packages built on the engine.

Lives in the package rather than under ``tests/`` because those other packages install the engine
from its wheel and cannot import its test tree.
"""
