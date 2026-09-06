"""Polymarket paper-trading runtime.

Import concrete services from their modules.  Keeping this package initializer
side-effect free prevents optional workers and legacy engines from becoming
hard runtime dependencies.
"""

__all__: list[str] = []
