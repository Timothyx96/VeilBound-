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


def resolve_eliminations(engine, max_steps: int = 64) -> None:
    """Auto-resolve any armed elimination by picking the first valid target.

    Full-match test loops have no interactive player; the authority still
    enforces every rule, the tests simply make the choice deterministically.
    The engine only arms a pause when at least one valid target exists, so
    the loop always makes progress.
    """
    for _ in range(max_steps):
        pending = engine.state.get("pending_elimination")
        if not pending:
            return
        targets = pending.get("valid_targets") or []
        assert targets, "an armed elimination must advertise valid targets"
        target = targets[0]
        result = engine.select_elimination_target(target["row_id"], target["slot_index"])
        assert result.accepted, result.reason
    raise AssertionError("elimination pauses did not resolve within the step limit")

