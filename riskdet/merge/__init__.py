"""Fusion and artifact emission."""

from .emit import emit  # noqa: F401
from .fuse import Candidate, expected_max_z, fuse  # noqa: F401

__all__ = ["Candidate", "fuse", "expected_max_z", "emit"]
