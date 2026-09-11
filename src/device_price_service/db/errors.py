"""Shared persistence error boundary, independent of database models."""


class RepositoryError(RuntimeError):
    """Base class for persistence errors with stable behavior."""
