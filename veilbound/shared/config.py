from dataclasses import dataclass


PLAYER_POOL_SIZE = 40
MATCH_ROUNDS = 2


@dataclass(frozen=True)
class GameConfig:
    deck_size: int = PLAYER_POOL_SIZE
    initial_hand_size: int = 5
    draw_per_turn: int = 1
    capacity_min: int = 1
    capacity_max: int = 10
    rounds_per_match: int = MATCH_ROUNDS

