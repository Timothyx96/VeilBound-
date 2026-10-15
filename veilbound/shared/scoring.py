"""Compatibility import for row scoring; game calculations live in shared.rules."""

from .rules import calculate_row_scores

__all__ = ["calculate_row_scores"]
