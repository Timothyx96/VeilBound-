import pytest

from veilbound.shared.config import GameConfig
from veilbound.shared.engine import GameEngine


@pytest.fixture
def engine():
    return GameEngine(seed=21)


def small_engine(deck_size=12, hand_size=5, draw_per_turn=1, seed=21):
    return GameEngine(
        GameConfig(deck_size=deck_size, initial_hand_size=hand_size, draw_per_turn=draw_per_turn),
        seed=seed,
    )

