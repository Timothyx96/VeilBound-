"""Compatibility imports for code written against the original engine module."""

from .rules import GameEngine, ROW_COUNT, calculate_row_scores, deal_player_pool

__all__ = ["GameEngine", "ROW_COUNT", "calculate_row_scores", "deal_player_pool"]
