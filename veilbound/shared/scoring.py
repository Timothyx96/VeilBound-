from __future__ import annotations

from .models import BattleRow
from .definitions import CardDefinition


def calculate_row_scores(row: BattleRow, player_ids: tuple[str, str], cards: dict[str, CardDefinition]) -> dict[str, int]:
    """Sum printed scores from revealed cards, ignoring empty and hidden slots."""
    scores = {player_id: 0 for player_id in player_ids}
    for card in row.slots:
        if card is not None and card.revealed:
            scores[card.owner_id] += cards[card.definition_id].base_points
    return scores

