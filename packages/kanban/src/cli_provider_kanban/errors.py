"""Errors for the kanban/Jev shadow package."""


class ShadowError(RuntimeError):
    """A shadow operation failed (missing board, over-limit scope, bad policy)."""


class CacheError(RuntimeError):
    """The decision cache file is corrupt; fails closed rather than guessing."""
