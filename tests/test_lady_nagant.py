from veilbound.shared.rules import GameEngine


def resolve_nagant(target_owner: str, target_base: int, target_bonus: int):
    engine = GameEngine(seed=1)
    state = engine.state
    nagant = engine._new_card("mha_07", "player-1")
    target = engine._new_card("naruto_01", target_owner)
    nagant.revealed = True
    target.revealed = True
    target.base_points = target_base
    target.current_points = target_base + target_bonus
    target.applied_bonuses = {"test_bonus": target_bonus} if target_bonus else {}
    row = state.rows[0]
    row.slots = [nagant, target, *([None] * (row.capacity - 2))]

    engine.abilities.on_card_revealed(
        state, nagant, row, 0, emit=engine._emit_ability_activation,
    )
    return engine, nagant, target, row


def test_nagant_deducts_three_bonus_first_then_base_points():
    _, _, target, _ = resolve_nagant("player-2", 5, 2)
    assert target.base_points == 4
    assert sum(target.bonus_point_reductions.values()) == 2
    assert target.current_points == 4

    _, _, target, _ = resolve_nagant("player-2", 5, 1)
    assert target.base_points == 3
    assert target.current_points == 3

    _, _, target, _ = resolve_nagant("player-2", 5, 0)
    assert target.base_points == 2
    assert target.current_points == 2


def test_nagant_does_not_affect_an_allied_next_card_and_resolves_once():
    engine, nagant, target, row = resolve_nagant("player-1", 5, 2)
    assert target.base_points == 5
    assert target.current_points == 7
    assert nagant.activated_abilities == ["mha_07_silent_sniper"]

    engine.abilities.on_card_revealed(
        engine.state, nagant, row, 0, emit=engine._emit_ability_activation,
    )
    assert target.base_points == 5
    assert target.current_points == 7
