"""Elimination mechanic tests: targeting rules, pause/resume, and sync.

Covers the required scenarios: any_row/same_row/type/base-point targeting,
point math, idempotency, the no-valid-targets failure, reveal pause/resume
during both final reveals and placement reveals, row scoring with an
eliminated card, once-per-match activation, and multiplayer view sync.
"""

from __future__ import annotations

import asyncio

from test_multiplayer import FakeWriter

from conftest import resolve_eliminations
from veilbound.client.renderer import Renderer
from veilbound.client.network import _state_from_view
from veilbound.shared.config import GameConfig
from veilbound.shared.engine import GameEngine
from veilbound.shared.elimination import EliminationManager, EliminationTargeting
from veilbound.shared.models import CardInstance, GameEvent, GamePhase
from veilbound.server.rooms import Room
from veilbound.shared.scoring import calculate_row_scores


KILLUA = "hxh_02"
GO = "naruto_01"        # 10 / GOAT
KAKASHI = "naruto_05"   # 7  / Fighter
ITACHI = "naruto_03"    # 6  / Dynamic Duo
HISOKA = "hxh_04"       # 8  / Fighter
LUFFY = "onepiece_01"   # 10 / GOAT
ZENO = "dragonball_09"
NICO_ROBIN = "onepiece_05"
NAMI = "onepiece_09"
CHOPPER = "onepiece_10"
GON = "hxh_01"
KILLUA = "hxh_02"
SASUKE = "naruto_02"
FILLERS = [GO, KAKASHI, ITACHI, HISOKA, LUFFY]


def scripted_engine(capacity=3, seed=4):
    """Engine with scrubbed pools so only scripted cards reach the board."""
    engine = GameEngine(
        GameConfig(
            deck_size=40, initial_hand_size=5, draw_per_turn=1,
            capacity_min=capacity, capacity_max=capacity, rounds_per_match=2,
        ),
        seed=seed,
    )
    serial = 0

    def _scrub(player):
        nonlocal serial
        for index in range(len(player.draw_pile)):
            serial += 1
            definition_id = FILLERS[index % len(FILLERS)]
            player.draw_pile[index] = CardInstance(
                f"filler-{serial}", definition_id, player.id,
                current_points=engine.cards[definition_id].base_points,
            )

    for player in engine.state.players:
        _scrub(player)
    return engine


def give(player, engine, *definition_ids):
    player.hand = []
    for definition_id in definition_ids:
        definition = engine.cards[definition_id]
        player.hand.append(CardInstance(
            f"{player.id}-{definition_id}-{len(player.hand)}",
            definition_id, player.id, current_points=definition.base_points,
        ))


def place(engine, card, row_index, slot_index):
    active = engine.state.active_player
    assert active.id == card.owner_id, "test sequence must follow turn order"
    result = engine.play_card(active.id, card.id, engine.state.rows[row_index].id, slot_index)
    assert result.accepted, result.reason
    return result


def card(engine, definition_id, owner_id):
    definition = engine.cards[definition_id]
    return CardInstance(
        f"unit-{definition_id}-{owner_id}", definition_id, owner_id,
        current_points=definition.base_points,
    )


def reveal_all_but(engine):
    """Reveal every placed card so only scripted hidden states remain."""
    for row in engine.state.rows:
        for slot in row.slots:
            if slot is not None and not slot.revealed:
                slot.revealed = True


def targets_of(manager, state, attacker, targeting):
    return [
        (row.id, index, placed.definition_id)
        for row, index, placed in manager.get_valid_targets(state, attacker, targeting)
    ]


def _arm_itachi_bonus_selection(engine, itachi, row):
    event_start = len(engine.state.events)
    engine._reveal_card(row, 0, itachi)
    return engine._begin_pending_elimination(event_start)


def _arm_nico_robin_selection(engine, robin, row):
    event_start = len(engine.state.events)
    engine._reveal_card(row, 0, robin)
    return engine._begin_pending_elimination(event_start)


def _arm_nami_selection(engine, nami, row):
    event_start = len(engine.state.events)
    engine._reveal_card(row, 0, nami)
    return engine._begin_pending_elimination(event_start)


def _arm_chopper_selection(engine, chopper, row, slot_index=0):
    event_start = len(engine.state.events)
    engine._reveal_card(row, slot_index, chopper)
    return engine._begin_pending_elimination(event_start)


# ---------------------------------------------------------------- targeting rules

def _board_with_two_sides():
    """Capacity-2 board: p1 holds row0/s0 + row1/s0, p2 holds row0/s1 + row1/s1."""
    engine = scripted_engine(capacity=2)
    p1, p2 = engine.state.players
    a = card(engine, KAKASHI, p1.id)    # 7 / Fighter, the attacker
    p1_filler = card(engine, ITACHI, p1.id)
    b = card(engine, GO, p2.id)         # 10 / GOAT
    c = card(engine, HISOKA, p2.id)     # 8 / Fighter
    p1.hand = [a, p1_filler]
    p2.hand = [b, c]
    place(engine, a, 0, 0)
    place(engine, b, 0, 1)          # reveals a
    place(engine, p1_filler, 1, 0)
    place(engine, c, 1, 1)          # reveals p1_filler
    reveal_all_but(engine)
    return engine, a, b, c


def test_any_row_scope_targets_opposing_revealed_cards_in_every_row():
    engine, attacker, _, _ = _board_with_two_sides()
    manager = EliminationManager(engine.cards)
    targeting = EliminationTargeting(target_scope="any_row", target_side="opposing")
    found = targets_of(manager, engine.state, attacker, targeting)
    assert [item[2] for item in found] == [GO, HISOKA]


def test_same_row_scope_only_targets_the_attackers_own_row():
    engine, attacker, _, _ = _board_with_two_sides()
    manager = EliminationManager(engine.cards)
    targeting = EliminationTargeting(target_scope="same_row", target_side="opposing")
    found = targets_of(manager, engine.state, attacker, targeting)
    assert [item[2] for item in found] == [GO]  # row0 only; HISOKA is row1


def test_allowed_types_filters_targets_to_matching_card_types():
    engine, attacker, _, _ = _board_with_two_sides()
    manager = EliminationManager(engine.cards)
    targeting = EliminationTargeting(
        target_scope="any_row", target_side="opposing", allowed_types=("Fighter",),
    )
    found = targets_of(manager, engine.state, attacker, targeting)
    assert [item[2] for item in found] == [HISOKA]  # GOAT GO filtered out


def test_max_base_points_excludes_stronger_targets():
    engine, attacker, _, _ = _board_with_two_sides()
    manager = EliminationManager(engine.cards)
    targeting = EliminationTargeting(
        target_scope="any_row", target_side="opposing", max_base_points=8,
    )
    found = targets_of(manager, engine.state, attacker, targeting)
    assert [item[2] for item in found] == [HISOKA]  # 10-point GO excluded


def test_min_base_points_excludes_weaker_targets():
    engine, attacker, _, _ = _board_with_two_sides()
    manager = EliminationManager(engine.cards)
    targeting = EliminationTargeting(
        target_scope="any_row", target_side="opposing", min_base_points=9,
    )
    found = targets_of(manager, engine.state, attacker, targeting)
    assert [item[2] for item in found] == [GO]  # 8-point HISOKA excluded


def test_own_side_hidden_and_eliminated_cards_are_never_eligible():
    engine, attacker, _, _ = _board_with_two_sides()
    manager = EliminationManager(engine.cards)
    state = engine.state
    p2 = state.players[1]
    # Same-owner cards are excluded even when revealed and strong.
    assert all(
        placed.owner_id == p2.id
        for _, _, placed in manager.get_valid_targets(state, attacker, EliminationTargeting())
    )
    # Hidden opposing cards are excluded.
    hidden = card(engine, LUFFY, p2.id)
    state.rows[1].slots[1] = hidden
    assert hidden.id not in {
        placed.id for _, _, placed in manager.get_valid_targets(
            state, attacker, EliminationTargeting(),
        )
    }
    # Already-eliminated cards drop out of the target list.
    b = next(
        slot for slot in state.rows[0].slots
        if slot is not None and slot.definition_id == GO
    )
    manager.eliminate_target(state, b, attacker)
    assert b.id not in {
        placed.id for _, _, placed in manager.get_valid_targets(
            state, attacker, EliminationTargeting(),
        )
    }


# ---------------------------------------------------------------- engine flow

def fill_rest(engine):
    """Place drawn fillers until placement ends; settle any pauses."""
    while engine.state.phase == GamePhase.PLACEMENT:
        resolve_eliminations(engine)
        if engine.state.phase != GamePhase.PLACEMENT:
            break
        active = engine.state.active_player
        row_id, slot_index = engine.legal_slots()[0]
        result = engine.play_card(active.id, active.hand[0].id, row_id, slot_index)
        assert result.accepted, result.reason
    resolve_eliminations(engine)


def test_killua_ability_parses_from_the_roster_json():
    engine = scripted_engine()
    ability = engine.cards[KILLUA].ability
    assert ability is not None
    assert ability.trigger == "on_reveal"
    assert [effect.type for effect in ability.effects] == ["eliminate_target"]
    assert ability.target_scope == "any_row"
    assert ability.target_side == "opposing"
    assert ability.presentation.animation == "elimination"
    assert ability.presentation.sound == "assets/sounds/Abillity_Sfx/Ability_Resolution/Killua.mp3"
    assert ability.activation_limit == "once_per_card_per_match"


def test_final_reveal_pause_resumes_after_selecting_a_target():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    give(p1, engine, GO, LUFFY, KILLUA)
    give(p2, engine, KAKASHI, HISOKA, ITACHI)
    go, luffy, killua = p1.hand
    kakashi, hisoka, itachi = p2.hand
    place(engine, go, 0, 0)              # GO (p1, hidden)
    place(engine, kakashi, 0, 1)         # KAKASHI reveals GO
    place(engine, killua, 0, 2)          # KILLUA reveals KAKASHI; row0 full
    place(engine, hisoka, 1, 0)
    place(engine, luffy, 1, 1)           # reveals HISOKA
    place(engine, itachi, 1, 2)          # reveals LUFFY
    fill_rest(engine)
    assert engine.state.phase == GamePhase.REVEAL

    reveal = engine.reveal_next_card()       # first hidden: row0/s2 = KILLUA
    assert reveal.accepted
    assert any(event.kind == "ability_activated" for event in reveal.events)
    assert not any(event.kind == "ability_resolved" for event in reveal.events)
    pending = engine.state.pending_elimination
    assert pending is not None
    assert pending["attacker_card_id"] == killua.id
    assert pending["attacker_owner_id"] == "player-1"
    # Row-major order includes the two scripted filler Fighters in rows 3 and 4.
    assert [(t["row_id"], t["slot_index"]) for t in pending["valid_targets"]] == [
        (engine.state.rows[0].id, 1),
        (engine.state.rows[1].id, 0),
        (engine.state.rows[2].id, 1),
        (engine.state.rows[3].id, 0),
    ]
    # The reveal flow is paused: another reveal is rejected until resolved.
    paused = engine.reveal_next_card()
    assert not paused.accepted
    assert "elimination" in paused.reason

    target = pending["valid_targets"][0]
    result = engine.select_elimination_target(target["row_id"], target["slot_index"])
    assert result.accepted, result.reason
    assert engine.state.pending_elimination is None
    eliminated = engine.state.rows[0].slots[1]
    assert eliminated.definition_id == KAKASHI
    assert eliminated.eliminated is True
    assert eliminated.current_points == 0
    assert engine.state.rows[0].eliminated_slots == [1]
    elimination_events = [event for event in result.events if event.kind == "elimination"]
    assert len(elimination_events) == 1
    assert elimination_events[0].data["attacker_name"] == "Killua Zoldyck"
    assert elimination_events[0].data["target_name"] == "Kakashi Hatake"
    resolved = [event for event in result.events if event.kind == "ability_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["sound"] == "assets/sounds/Abillity_Sfx/Ability_Resolution/Killua.mp3"
    assert resolved[0].data["target_card_id"] == eliminated.id

    # Reveal flow resumes and finishes the round normally.
    while engine.state.phase == GamePhase.REVEAL:
        assert engine.reveal_next_card().accepted
    assert engine.state.phase == GamePhase.ROW_SCORING


def test_no_valid_elimination_target_emits_no_successful_resolution():
    engine = scripted_engine(capacity=2)
    p1, p2 = engine.state.players
    give(p1, engine, KILLUA)
    give(p2, engine, KAKASHI)
    attacker = p1.hand[0]
    hidden_opponent = p2.hand[0]

    place(engine, attacker, 0, 0)
    result = place(engine, hidden_opponent, 0, 1)

    assert any(event.kind == "ability_activated" for event in result.events)
    assert any(event.kind == "elimination_failed" for event in result.events)
    assert not any(event.kind == "ability_resolved" for event in result.events)
    assert engine.state.pending_elimination is None


def test_cancelling_pending_elimination_emits_no_successful_resolution():
    engine = scripted_engine(capacity=2)
    p1, p2 = engine.state.players
    attacker = card(engine, KILLUA, p1.id)
    target = card(engine, KAKASHI, p2.id)
    target.revealed = True
    row = engine.state.rows[0]
    row.slots[0] = target
    row.slots[1] = attacker
    engine.state.phase = GamePhase.REVEAL
    engine.state.final_reveal_active = True

    reveal = engine.reveal_next_card()
    assert reveal.accepted
    assert engine.state.pending_elimination is not None
    cancelled = engine.fail_elimination()

    assert cancelled.accepted
    assert any(event.kind == "elimination_failed" for event in cancelled.events)
    assert not any(event.kind == "ability_resolved" for event in cancelled.events)


def _itachi_bonus_target_fixture():
    engine = scripted_engine(capacity=4)
    p1, p2 = engine.state.players
    itachi = card(engine, ITACHI, p1.id)
    target = card(engine, GO, p2.id)
    target.revealed = True
    target.applied_bonuses = {"bonus_a": 3, "bonus_b": 2}
    target.current_points = engine.cards[GO].base_points + 5
    unboosted = card(engine, HISOKA, p2.id)
    unboosted.revealed = True
    friendly = card(engine, GO, p1.id)
    friendly.revealed = True
    friendly.applied_bonuses = {"friendly_bonus": 4}
    friendly.current_points += 4
    hidden = card(engine, HISOKA, p2.id)
    hidden.current_points += 4
    eliminated = card(engine, GO, p2.id)
    eliminated.revealed = True
    eliminated.eliminated = True
    eliminated.current_points = 0
    other_row_target = card(engine, HISOKA, p2.id)
    other_row_target.revealed = True
    other_row_target.current_points += 4

    first_row = engine.state.rows[0]
    first_row.slots[:] = [itachi, target, unboosted, friendly]
    second_row = engine.state.rows[1]
    second_row.slots[0] = hidden
    second_row.slots[1] = eliminated
    second_row.slots[2] = other_row_target
    return engine, itachi, target, unboosted, friendly, hidden, eliminated, other_row_target


def test_itachi_tsukuyomi_removes_all_bonus_points_without_eliminating_target():
    (engine, itachi, target, _unboosted, _friendly, _hidden, _eliminated,
     _other_row_target) = _itachi_bonus_target_fixture()
    row = engine.state.rows[0]
    assert _arm_itachi_bonus_selection(engine, itachi, row)
    pending = engine.state.pending_elimination
    assert pending["effect_type"] == "remove_target_bonus"
    assert pending["floating_text"] == "TSUKUYOMI"
    assert pending["valid_targets"] == [{"row_id": row.id, "slot_index": 1, "card_id": target.id}]

    result = engine.select_elimination_target(row.id, 1)

    assert result.accepted
    assert target.current_points == engine.cards[GO].base_points
    assert target.applied_bonuses == {}
    assert target.revealed and not target.eliminated
    assert row.eliminated_slots == []
    assert not any(event.kind == "elimination" for event in result.events)
    resolved = [event for event in result.events if event.kind == "ability_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["floating_text"] == "TSUKUYOMI"
    assert not engine.select_elimination_target(row.id, 1).accepted
    assert sum(event.kind == "ability_resolved" for event in engine.state.events) == 1
    scores = calculate_row_scores(row, tuple(player.id for player in engine.state.players), engine.cards)
    assert scores[target.owner_id] == (
        engine.cards[GO].base_points + engine.cards[HISOKA].base_points
    )
    assert "itachi_tsukuyomi_remove_bonus" in itachi.activated_abilities


def test_itachi_revalidates_friendly_other_row_unboosted_hidden_and_eliminated_targets():
    (engine, itachi, target, unboosted, friendly, hidden, eliminated,
     other_row_target) = _itachi_bonus_target_fixture()
    first_row = engine.state.rows[0]
    assert _arm_itachi_bonus_selection(engine, itachi, first_row)

    for row, slot_index in (
        (first_row, 2),  # opposing, but no bonus
        (first_row, 3),  # friendly, despite having a bonus
        (engine.state.rows[1], 0),  # unrevealed
        (engine.state.rows[1], 1),  # eliminated
        (engine.state.rows[1], 2),  # other row
    ):
        result = engine.select_elimination_target(row.id, slot_index)
        assert not result.accepted
    assert unboosted.current_points == engine.cards[HISOKA].base_points
    assert friendly.current_points > engine.cards[GO].base_points
    assert hidden.current_points > engine.cards[HISOKA].base_points
    assert eliminated.eliminated and eliminated.current_points == 0
    assert other_row_target.current_points > engine.cards[HISOKA].base_points
    assert target.current_points > engine.cards[GO].base_points
    assert engine.state.pending_elimination is not None

    assert engine.select_elimination_target(first_row.id, 1).accepted


def test_itachi_skips_selection_when_no_bonus_target_exists():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    itachi = card(engine, ITACHI, p1.id)
    opponent = card(engine, GO, p2.id)
    opponent.revealed = True
    row = engine.state.rows[0]
    row.slots[0] = itachi
    row.slots[1] = opponent

    assert not _arm_itachi_bonus_selection(engine, itachi, row)

    assert engine.state.pending_elimination is None
    assert opponent.current_points == engine.cards[GO].base_points
    assert not any(event.kind == "ability_resolved" for event in engine.state.events)
    assert not any(event.kind == "elimination_failed" for event in engine.state.events)
    failed = [event for event in engine.state.events if event.kind == "ability_failed"]
    assert len(failed) == 1
    assert failed[0].message == "Itachi Uchiha's Tsukuyomi failed"
    # The normal placement flow remains available when no target exists.
    give(p1, engine, GO)
    result = engine.play_card(p1.id, p1.hand[0].id, engine.state.rows[1].id, 0)
    assert result.accepted


def test_itachi_cancelled_target_selection_reports_ability_failure():
    (engine, itachi, _target, _unboosted, _friendly, _hidden, _eliminated,
     _other_row_target) = _itachi_bonus_target_fixture()
    row = engine.state.rows[0]
    assert _arm_itachi_bonus_selection(engine, itachi, row)

    result = engine.fail_elimination()

    assert result.accepted
    failed = [event for event in result.events if event.kind == "ability_failed"]
    assert len(failed) == 1
    assert failed[0].message == "Itachi Uchiha's Tsukuyomi failed"
    assert failed[0].data["ability_id"] == "itachi_tsukuyomi_remove_bonus"
    assert not any(event.kind == "ability_resolved" for event in result.events)


def test_itachi_bonus_removal_suppresses_active_continuous_bonus_until_its_condition_ends():
    engine = scripted_engine(capacity=4)
    p1, p2 = engine.state.players
    itachi = card(engine, ITACHI, p1.id)
    naruto = card(engine, "naruto_01", p2.id)
    hinata = card(engine, "naruto_10", p2.id)
    naruto.revealed = True
    hinata.revealed = True
    row = engine.state.rows[0]
    row.slots[:] = [itachi, naruto, hinata, None]
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert naruto.current_points == engine.cards[naruto.definition_id].base_points + 3
    assert _arm_itachi_bonus_selection(engine, itachi, row)

    assert engine.select_elimination_target(row.id, 1).accepted
    engine.abilities.recalculate_continuous_bonuses(engine.state)

    assert naruto.current_points == engine.cards[naruto.definition_id].base_points
    assert naruto.applied_bonuses == {}
    assert "naruto_hinata_bond_of_love" in naruto.suppressed_bonuses


def test_itachi_bonus_removal_synchronizes_to_both_multiplayer_views_and_owner_selects():
    (engine, itachi, target, _unboosted, _friendly, _hidden, _eliminated,
     _other_row_target) = _itachi_bonus_target_fixture()
    row = engine.state.rows[0]
    assert _arm_itachi_bonus_selection(engine, itachi, row)
    room = Room("TSUKUYOMI")
    room.engine = engine
    writer1, writer2 = FakeWriter(), FakeWriter()
    room.peers["player-1"].writer = writer1
    room.peers["player-2"].writer = writer2

    async def choose():
        await room.handle(room.peers["player-2"], {
            "type": "select_elimination", "row_id": row.id, "slot_index": 1,
        })
        assert engine.state.pending_elimination is not None
        await room.handle(room.peers["player-1"], {
            "type": "select_elimination", "row_id": row.id, "slot_index": 1,
        })

    asyncio.run(choose())

    assert writer2.errors()
    assert engine.state.pending_elimination is None
    for player_id in ("player-1", "player-2"):
        view = engine.view_for(player_id)
        card_view = view["rows"][0]["slots"][1]["card"]
        assert card_view["current_points"] == engine.cards[GO].base_points
        projected = _state_from_view(view)
        assert projected.rows[0].slots[1].current_points == engine.cards[GO].base_points
    assert target.revealed and not target.eliminated


def _nico_robin_board(target_base: int, target_bonus: int = 0):
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    robin = card(engine, NICO_ROBIN, p1.id)
    target = card(engine, GO, p2.id)
    target.base_points = target_base
    target.current_points = target_base + target_bonus
    if target_bonus:
        target.applied_bonuses = {"test_existing_bonus": target_bonus}
    row = engine.state.rows[0]
    row.slots[0] = robin
    row.slots[1] = target
    target.revealed = True
    return engine, robin, target, row


def test_nico_robin_reduces_instance_base_points_and_clamps_at_zero():
    for original, expected in ((8, 6), (5, 3), (2, 0), (1, 0)):
        engine, robin, target, row = _nico_robin_board(original)
        assert _arm_nico_robin_selection(engine, robin, row)
        pending = engine.state.pending_elimination
        assert pending["effect_type"] == "reduce_target_base_points"
        assert pending["targeting"]["target_side"] == "opposing"
        assert {(item["row_id"], item["slot_index"]) for item in pending["valid_targets"]} == {
            (row.id, 1),
        }

        result = engine.select_elimination_target(row.id, 1)

        assert result.accepted
        assert engine.abilities._base_points(target) == expected
        assert target.current_points == expected
        assert target.eliminated is (expected == 0)
        if expected == 0:
            death = next(event for event in result.events if event.kind == "death")
            assert death.message == "Naruto Uzumaki Died"
        assert engine.cards[GO].base_points == 10  # roster definition stays immutable
        assert len(target.base_point_reductions) == 1
        assert sum(event.kind == "ability_resolved" for event in result.events) == 1
        assert not engine.abilities.reduce_base_points(
            target, next(iter(target.base_point_reductions)), 2,
        )
        assert target.base_points == expected
        scores = calculate_row_scores(row, tuple(player.id for player in engine.state.players), engine.cards)
        assert scores[target.owner_id] == expected
        # The authoritative projection and multiplayer reconstruction carry the reduced base.
        for player in engine.state.players:
            view = engine.view_for(player.id)
            target_view = view["rows"][0]["slots"][1]["card"]
            assert target_view["base_points"] == expected
            assert target_view["current_points"] == expected
            projected = _state_from_view(view)
            assert projected.rows[0].slots[1].base_points == expected
            assert projected.rows[0].slots[1].base_point_reductions == target.base_point_reductions


def test_nico_robin_preserves_bonus_and_recalculates_reduced_base_after_duo_elimination():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    robin = card(engine, NICO_ROBIN, p1.id)
    sasuke = card(engine, SASUKE, p2.id)
    itachi = card(engine, ITACHI, p2.id)
    row = engine.state.rows[0]
    row.slots[:] = [robin, sasuke, itachi]
    robin.revealed = False
    sasuke.revealed = True
    itachi.revealed = True
    sasuke.applied_bonuses = {f"sasuke_shared_row_bonus:{sasuke.id}": 3}
    sasuke.current_points = engine.cards[SASUKE].base_points + 3

    assert _arm_nico_robin_selection(engine, robin, row)
    assert engine.select_elimination_target(row.id, 1).accepted
    assert sasuke.base_points == 5
    assert sasuke.current_points == 8
    assert sasuke.applied_bonuses == {f"sasuke_shared_row_bonus:{sasuke.id}": 3}

    engine.elimination.eliminate_target(engine.state, itachi, robin)
    engine.abilities.recalculate_after_elimination(engine.state, itachi)

    assert sasuke.base_points == 5
    assert sasuke.current_points == 5
    assert sasuke.applied_bonuses == {}
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sasuke.base_points == 5
    assert sasuke.current_points == 5
    assert len(sasuke.base_point_reductions) == 1


def test_nico_robin_revalidates_targets_and_cancellation_does_not_reduce_points():
    engine, robin, target, row = _nico_robin_board(8)
    hidden = card(engine, KAKASHI, engine.state.players[0].id)
    hidden.revealed = False
    eliminated = card(engine, HISOKA, engine.state.players[1].id)
    eliminated.revealed = True
    eliminated.eliminated = True
    eliminated.current_points = 0
    second_row = engine.state.rows[1]
    row.slots[2] = hidden
    second_row.slots[0] = eliminated
    assert _arm_nico_robin_selection(engine, robin, row)

    assert not engine.select_elimination_target(row.id, 2).accepted
    assert not engine.select_elimination_target(second_row.id, 0).accepted
    assert engine.state.pending_elimination is not None
    assert engine.fail_elimination().accepted
    assert engine.state.pending_elimination is None
    assert target.base_points == 8
    assert target.current_points == 8
    assert target.base_point_reductions == {}


def test_nico_robin_only_targets_opposing_cards_and_syncs_reduced_base():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    robin = card(engine, NICO_ROBIN, p1.id)
    friendly = card(engine, GO, p1.id)
    opposing = card(engine, HISOKA, p2.id)
    row = engine.state.rows[0]
    row.slots[:] = [robin, friendly, None]
    engine.state.rows[1].slots[0] = opposing
    friendly.revealed = True
    opposing.revealed = True
    assert _arm_nico_robin_selection(engine, robin, row)
    assert {item["card_id"] for item in engine.state.pending_elimination["valid_targets"]} == {
        opposing.id,
    }

    room = Room("NICO-ROBIN")
    room.engine = engine
    writer1, writer2 = FakeWriter(), FakeWriter()
    room.peers[p1.id].writer = writer1
    room.peers[p2.id].writer = writer2

    async def choose():
        assert not engine.select_elimination_target(row.id, 1).accepted
        await room.handle(room.peers[p1.id], {
            "type": "select_elimination", "row_id": row.id, "slot_index": 1,
        })
        assert engine.state.pending_elimination is not None
        await room.handle(room.peers[p2.id], {
            "type": "select_elimination", "row_id": engine.state.rows[1].id, "slot_index": 0,
        })
        assert engine.state.pending_elimination is not None
        await room.handle(room.peers[p1.id], {
            "type": "select_elimination", "row_id": engine.state.rows[1].id, "slot_index": 0,
        })

    asyncio.run(choose())

    assert writer2.errors()
    assert opposing.base_points == 6
    assert opposing.current_points == 6
    for player_id in (p1.id, p2.id):
        opposing_view = engine.view_for(player_id)["rows"][1]["slots"][0]["card"]
        assert opposing_view["base_points"] == 6
        assert opposing_view["current_points"] == 6


def test_nico_robin_eliminating_gon_removes_gons_fighter_bonus_in_shared_state():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    robin = card(engine, NICO_ROBIN, p1.id)
    gon = card(engine, "hxh_01", p2.id)
    fighter = card(engine, HISOKA, p2.id)
    row = engine.state.rows[0]
    row.slots[0] = robin
    row.slots[1] = gon
    engine.state.rows[1].slots[0] = fighter
    fighter.revealed = True

    engine._reveal_card(row, 1, gon)
    gon_bonus_key = f"gon_jajanken_rock_ultimate_transformation:{gon.id}"
    assert gon.base_points == 1
    assert fighter.applied_bonuses[gon_bonus_key] == 4
    assert fighter.current_points == engine.cards[HISOKA].base_points + 4

    assert _arm_nico_robin_selection(engine, robin, row)
    result = engine.select_elimination_target(row.id, 1)

    assert result.accepted
    assert gon.base_points == 0 and gon.eliminated and gon.current_points == 0
    assert fighter.eliminated is False
    assert gon_bonus_key not in fighter.applied_bonuses
    assert fighter.current_points == engine.cards[HISOKA].base_points
    assert any(event.kind == "death" and event.message == "Gon Freecss Died" for event in result.events)
    for player in engine.state.players:
        view = engine.view_for(player.id)
        gon_view = view["rows"][0]["slots"][1]["card"]
        fighter_view = view["rows"][1]["slots"][0]["card"]
        assert gon_view["eliminated"] and gon_view["current_points"] == 0
        assert fighter_view["current_points"] == engine.cards[HISOKA].base_points
        assert gon_bonus_key not in fighter_view["applied_bonuses"]


def _nami_theft_fixture(target_bonus: int):
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    nami = card(engine, NAMI, p1.id)
    target = card(engine, GO, p2.id)
    target.revealed = True
    target_bonus_key = "preexisting_bonus"
    if target_bonus:
        target.applied_bonuses = {target_bonus_key: target_bonus}
    target.current_points = engine.cards[GO].base_points + target_bonus
    row = engine.state.rows[0]
    row.slots[0] = nami
    row.slots[1] = target
    return engine, nami, target, row, target_bonus_key


def test_nami_steals_at_most_two_bonus_points_without_touching_base_points():
    for available, expected in ((1, 1), (2, 2), (4, 2)):
        engine, nami, target, row, target_bonus_key = _nami_theft_fixture(available)
        nami_base = engine.cards[NAMI].base_points
        target_base = engine.cards[GO].base_points
        assert _arm_nami_selection(engine, nami, row)
        pending = engine.state.pending_elimination
        assert pending["effect_type"] == "steal_target_bonus"
        assert pending["targeting"]["target_scope"] == "same_row"
        assert pending["targeting"]["target_side"] == "opposing"
        assert pending["targeting"]["require_bonus_points"] is True
        assert [item["card_id"] for item in pending["valid_targets"]] == [target.id]

        result = engine.select_elimination_target(row.id, 1)

        assert result.accepted
        stolen = min(available, 2)
        assert nami.current_points == nami_base + stolen
        assert target.current_points == target_base + available - stolen
        assert engine.abilities._base_points(nami) == nami_base
        assert engine.abilities._base_points(target) == target_base
        assert engine.cards[NAMI].base_points == nami_base
        assert engine.cards[GO].base_points == target_base
        assert nami.applied_bonuses[f"nami_cat_burglar_steal_bonus:{nami.id}"] == stolen
        assert target.applied_bonuses[target_bonus_key] == available
        assert sum(value for key, value in target.applied_bonuses.items() if key.startswith("nami_cat_burglar_steal_bonus:")) == -stolen
        assert target.revealed and not target.eliminated
        assert sum(event.kind == "ability_resolved" for event in result.events) == 1
        for _ in range(2):
            engine.abilities.recalculate_continuous_bonuses(engine.state)
        assert nami.current_points == nami_base + stolen
        assert target.current_points == target_base + available - stolen
        scores = calculate_row_scores(row, tuple(player.id for player in engine.state.players), engine.cards)
        assert scores[nami.owner_id] == nami.current_points
        assert scores[target.owner_id] == target.current_points
        for player in engine.state.players:
            view = engine.view_for(player.id)
            nami_view = view["rows"][0]["slots"][0]["card"]
            target_view = view["rows"][0]["slots"][1]["card"]
            assert nami_view["current_points"] == nami.current_points
            assert target_view["current_points"] == target.current_points
            assert target_view["applied_bonuses"] == target.applied_bonuses


def test_nami_cannot_select_or_resolve_a_card_without_bonus_points():
    engine, nami, target, row, _target_bonus_key = _nami_theft_fixture(0)

    assert not _arm_nami_selection(engine, nami, row)
    assert engine.state.pending_elimination is None
    assert not engine.select_elimination_target(row.id, 1).accepted
    assert target.current_points == engine.cards[GO].base_points
    assert target.base_point_reductions == {}
    assert not any(
        event.kind == "ability_resolved"
        and event.data.get("ability_id") == "nami_cat_burglar_steal_bonus"
        for event in engine.state.events
    )

    # Eligibility is checked again against live authoritative state at selection.
    engine, nami, target, row, _target_bonus_key = _nami_theft_fixture(2)
    assert _arm_nami_selection(engine, nami, row)
    target.current_points = engine.cards[GO].base_points
    target.applied_bonuses = {}
    result = engine.select_elimination_target(row.id, 1)
    assert not result.accepted
    assert engine.state.pending_elimination is not None
    assert not any(
        event.kind == "ability_resolved"
        and event.data.get("ability_id") == "nami_cat_burglar_steal_bonus"
        for event in result.events
    )


def test_nami_bonus_steal_is_authoritative_for_multiplayer():
    engine, nami, target, row, _target_bonus_key = _nami_theft_fixture(3)
    assert _arm_nami_selection(engine, nami, row)
    room = Room("NAMI-STEAL")
    room.engine = engine
    writer1, writer2 = FakeWriter(), FakeWriter()
    room.peers[nami.owner_id].writer = writer1
    room.peers[target.owner_id].writer = writer2

    async def choose_target():
        await room.handle(room.peers[nami.owner_id], {
            "type": "select_elimination", "row_id": row.id, "slot_index": 1,
        })

    asyncio.run(choose_target())

    assert engine.state.pending_elimination is None
    assert nami.current_points == engine.cards[NAMI].base_points + 2
    assert target.current_points == engine.cards[GO].base_points + 1
    assert writer1.messages and writer2.messages
    for player in engine.state.players:
        view = engine.view_for(player.id)
        assert view["rows"][0]["slots"][0]["card"]["current_points"] == nami.current_points
        assert view["rows"][0]["slots"][1]["card"]["current_points"] == target.current_points


def test_chopper_revives_only_an_eliminated_friendly_card_in_the_same_row():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    chopper = card(engine, CHOPPER, p1.id)
    friendly_dead = card(engine, KILLUA, p1.id)
    enemy_dead = card(engine, GO, p2.id)
    friendly_dead.revealed = True
    friendly_dead.eliminated = True
    friendly_dead.current_points = 0
    enemy_dead.revealed = True
    enemy_dead.eliminated = True
    enemy_dead.current_points = 0
    row = engine.state.rows[0]
    row.slots[:] = [chopper, friendly_dead, enemy_dead]
    row.eliminated_slots = [1, 2]

    assert _arm_chopper_selection(engine, chopper, row)
    pending = engine.state.pending_elimination
    assert pending["effect_type"] == "revive_target"
    assert pending["targeting"]["target_side"] == "friendly"
    assert pending["targeting"]["require_eliminated"] is True
    assert pending["valid_targets"] == [{
        "row_id": row.id, "slot_index": 1, "card_id": friendly_dead.id,
    }]

    result = engine.select_elimination_target(row.id, 1)

    assert result.accepted
    assert not friendly_dead.eliminated
    assert friendly_dead.current_points == engine.cards[KILLUA].base_points
    assert row.slots[1] is friendly_dead
    assert row.eliminated_slots == [2]
    assert "chopper_doctor_revive_friendly" in chopper.resolved_abilities
    assert not any(event.kind == "elimination" for event in result.events)
    for player in (p1, p2):
        view = engine.view_for(player.id)
        revived_view = view["rows"][0]["slots"][1]["card"]
        assert revived_view["eliminated"] is False
        assert revived_view["current_points"] == engine.cards[KILLUA].base_points
        assert revived_view["resolved_abilities"] == []


def test_chopper_restores_gon_after_robin_eliminates_transformed_gon():
    engine = scripted_engine(capacity=4)
    p1, p2 = engine.state.players
    robin = card(engine, NICO_ROBIN, p1.id)
    gon = card(engine, GON, p2.id)
    chopper = card(engine, CHOPPER, p2.id)
    fighter = card(engine, HISOKA, p2.id)
    fighter.revealed = True
    row = engine.state.rows[0]
    row.slots[:] = [robin, gon, chopper, None]
    engine.state.rows[1].slots[0] = fighter

    # Gon transforms and grants the fighter +4 before Robin reduces Gon to 0.
    engine._reveal_card(row, 1, gon)
    gon_ability = "gon_jajanken_rock_ultimate_transformation"
    gon_bonus_key = f"{gon_ability}:{gon.id}"
    assert gon.base_points == gon.current_points == 1
    assert gon_ability in gon.resolved_abilities
    assert fighter.applied_bonuses[gon_bonus_key] == 4

    assert _arm_nico_robin_selection(engine, robin, row)
    assert engine.select_elimination_target(row.id, 1).accepted
    assert gon.eliminated and gon.base_points == 0
    assert gon_bonus_key not in fighter.applied_bonuses
    assert fighter.current_points == engine.cards[HISOKA].base_points

    assert _arm_chopper_selection(engine, chopper, row, slot_index=2)
    result = engine.select_elimination_target(row.id, 1)

    assert result.accepted
    assert not gon.eliminated
    # His special revival reset restores the GOAT card first, then makes one
    # explicit attempt because an eligible Fighter is already present.
    assert gon.base_points == gon.current_points == 1
    assert gon.base_point_reductions == {}
    assert gon.applied_bonuses == {}
    assert gon.activated_abilities == [gon_ability]
    assert gon.resolved_abilities == [gon_ability]
    assert row.eliminated_slots == []
    definition = engine.cards[GON]
    assert definition.base_points == 10
    assert definition.full_card_art == "assets/art/card front/HxH/Gon Freecss_ Gouden GOAT-kaart.png"
    assert Renderer._activated_transformation_art(definition, gon) is None
    assert fighter.applied_bonuses[gon_bonus_key] == 4
    assert fighter.current_points == engine.cards[HISOKA].base_points + 4

    # Both authoritative views expose the post-revival activation and bonus.
    for player in engine.state.players:
        gon_view = engine.view_for(player.id)["rows"][0]["slots"][1]["card"]
        assert gon_view["definition_id"] == GON
        assert gon_view["base_points"] == gon_view["current_points"] == 1
        assert gon_view["eliminated"] is False
        assert gon_ability in gon_view["activated_abilities"]
        assert gon_ability in gon_view["resolved_abilities"]
        assert engine.view_for(player.id)["rows"][1]["slots"][0]["card"]["applied_bonuses"][gon_bonus_key] == 4

    scores = calculate_row_scores(row, tuple(player.id for player in engine.state.players), engine.cards)
    assert scores[p2.id] == gon.current_points + chopper.current_points

    # A later reveal cannot repeat the successful revival attempt.
    new_fighter = card(engine, HISOKA, p2.id)
    engine.state.rows[2].slots[0] = new_fighter
    engine._reveal_card(engine.state.rows[2], 0, new_fighter)
    assert gon.base_points == gon.current_points == 1
    assert gon_bonus_key not in new_fighter.applied_bonuses


def test_chopper_revives_pre_activation_gon_and_allows_a_fresh_activation():
    engine = scripted_engine(capacity=4)
    p1, p2 = engine.state.players
    zeno = card(engine, ZENO, p2.id)
    gon = card(engine, GON, p1.id)
    chopper = card(engine, CHOPPER, p1.id)
    new_fighter = card(engine, HISOKA, p1.id)
    row = engine.state.rows[0]
    row.slots[:] = [zeno, gon, chopper, new_fighter]

    # Zeno eliminates unrevealed Gon before his ability has ever activated.
    assert engine._resolve_zeno_trap(row, gon, 1)
    assert gon.eliminated and not gon.revealed
    assert gon.activated_abilities == [] and gon.resolved_abilities == []

    assert _arm_chopper_selection(engine, chopper, row, slot_index=2)
    revived = engine.select_elimination_target(row.id, 1)
    assert revived.accepted
    assert not gon.eliminated and not gon.revealed
    assert gon.base_points == gon.current_points == 10
    assert gon.activated_abilities == [] and gon.resolved_abilities == []
    assert Renderer._activated_transformation_art(engine.cards[GON], gon) is None

    # Gon can now reveal and resolve again; the new activation grants the new
    # snapshot once, without restoring any bonus from before his revival.
    _reveal_before = len(engine.state.events)
    engine._reveal_card(row, 1, gon)
    ability_id = "gon_jajanken_rock_ultimate_transformation"
    new_bonus_key = f"{ability_id}:{gon.id}"
    assert gon.base_points == gon.current_points == 1
    assert ability_id in gon.resolved_abilities
    assert new_fighter.applied_bonuses[new_bonus_key] == 4
    assert new_fighter.current_points == engine.cards[HISOKA].base_points + 4
    assert sum(
        event.kind == "ability_resolved" and event.data.get("ability_id") == ability_id
        for event in engine.state.events[_reveal_before:]
    ) == 1


def test_gon_no_target_attempt_retries_after_killua_elimination_and_chopper_revival():
    engine = scripted_engine(capacity=4)
    p1, p2 = engine.state.players
    gon = card(engine, GON, p1.id)
    killua = card(engine, KILLUA, p2.id)
    chopper = card(engine, CHOPPER, p1.id)
    fighter = card(engine, HISOKA, p1.id)
    row = engine.state.rows[0]
    row.slots[:] = [gon, killua, chopper, None]

    gon_ability = "gon_jajanken_rock_ultimate_transformation"
    # Gon is revealed before any Fighter exists, so the effect fails cleanly
    # and stays pending without consuming his once-per-match activation.
    engine._reveal_card(row, 0, gon)
    assert engine.abilities._base_points(gon) == gon.current_points == 10
    assert gon_ability not in gon.activated_abilities
    assert gon_ability not in gon.resolved_abilities
    assert any(
        ability_id == gon_ability and owner_id == gon.id
        for _, ability_id, owner_id in engine.abilities.pending_abilities
    )
    assert not fighter.applied_bonuses

    # Killua reveals and selects Gon. Elimination keeps the unresolved pending
    # ability available while Gon is absent from active play.
    event_start = len(engine.state.events)
    engine._reveal_card(row, 1, killua)
    assert engine._begin_pending_elimination(event_start)
    killed = engine.select_elimination_target(row.id, 0)
    assert killed.accepted and gon.eliminated
    assert gon_ability not in gon.resolved_abilities

    engine.state.rows[1].slots[0] = fighter
    fighter.revealed = True
    # A Fighter is now on the board before Chopper restores Gon, so revival is
    # the eligible retry event and applies the ability immediately.
    assert _arm_chopper_selection(engine, chopper, row, slot_index=2)
    revived = engine.select_elimination_target(row.id, 0)
    assert revived.accepted
    assert not gon.eliminated
    assert gon.base_points == gon.current_points == 1
    assert gon.activated_abilities == [gon_ability]
    assert gon.resolved_abilities == [gon_ability]
    assert Renderer._activated_transformation_art(engine.cards[GON], gon) is None
    gon_bonus_key = f"{gon_ability}:{gon.id}"
    assert fighter.applied_bonuses[gon_bonus_key] == 4
    assert not any(
        ability_id == gon_ability and owner_id == gon.id
        for _, ability_id, owner_id in engine.abilities.pending_abilities
    )

    # Revealing another Fighter afterward must not retry or duplicate Gon.
    later_fighter = card(engine, HISOKA, p1.id)
    engine.state.rows[2].slots[0] = later_fighter
    resolve_start = len(engine.state.events)
    engine._reveal_card(engine.state.rows[2], 0, later_fighter)
    assert gon.base_points == gon.current_points == 1
    assert gon_ability in gon.activated_abilities
    assert gon_ability in gon.resolved_abilities
    assert fighter.applied_bonuses[gon_bonus_key] == 4
    assert later_fighter.current_points == engine.cards[HISOKA].base_points
    assert sum(
        event.kind == "ability_resolved" and event.data.get("ability_id") == gon_ability
        for event in engine.state.events[resolve_start:]
    ) == 0
    assert sum(event.kind == "ability_activated" and event.data.get("ability_id") == gon_ability
               for event in revived.events) == 1
    for player in engine.state.players:
        view = engine.view_for(player.id)
        gon_view = view["rows"][0]["slots"][0]["card"]
        fighter_view = view["rows"][1]["slots"][0]["card"]
        assert gon_view["base_points"] == gon_view["current_points"] == 1
        assert gon_ability in gon_view["resolved_abilities"]
        assert fighter_view["current_points"] == engine.cards[HISOKA].base_points + 4
        assert fighter_view["applied_bonuses"][gon_bonus_key] == 4


def test_chopper_without_valid_revive_target_fails_without_consuming_ability():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    chopper = card(engine, CHOPPER, p1.id)
    living_friend = card(engine, KILLUA, p1.id)
    eliminated_enemy = card(engine, GO, p2.id)
    living_friend.revealed = True
    eliminated_enemy.revealed = True
    eliminated_enemy.eliminated = True
    eliminated_enemy.current_points = 0
    row = engine.state.rows[0]
    row.slots[:] = [chopper, living_friend, eliminated_enemy]

    assert not _arm_chopper_selection(engine, chopper, row)
    assert engine.state.pending_elimination is None
    assert "chopper_doctor_revive_friendly" not in chopper.activated_abilities
    assert "chopper_doctor_revive_friendly" not in chopper.resolved_abilities
    assert any(
        event.kind == "ability_failed"
        and event.data.get("ability_id") == "chopper_doctor_revive_friendly"
        for event in engine.state.events
    )


def test_killua_can_retry_after_failed_attempt_and_success_stays_used_after_revival():
    engine = scripted_engine(capacity=5)
    p1, p2 = engine.state.players
    chopper_one = card(engine, CHOPPER, p1.id)
    killua = card(engine, KILLUA, p1.id)
    target = card(engine, LUFFY, p2.id)
    chopper_two = card(engine, CHOPPER, p1.id)
    row = engine.state.rows[0]
    row.slots[:] = [chopper_one, killua, target, chopper_two, None]

    # Killua's first attempt fails because the only opposing card is hidden.
    event_start = len(engine.state.events)
    engine._reveal_card(row, 1, killua)
    assert not engine._begin_pending_elimination(event_start)
    killua_ability_id = engine.cards[KILLUA].abilities[0].id
    assert killua_ability_id not in killua.activated_abilities
    assert killua_ability_id not in killua.resolved_abilities

    engine._reveal_card(row, 2, target)
    engine.elimination.eliminate_target(engine.state, killua, target)
    assert _arm_chopper_selection(engine, chopper_one, row)
    revived = engine.select_elimination_target(row.id, 1)
    assert revived.accepted
    assert not killua.eliminated
    # Revival re-attempts Killua's unresolved on-reveal ability.
    assert engine.state.pending_elimination is not None
    assert engine.state.pending_elimination["attacker_card_id"] == killua.id
    assert killua_ability_id not in killua.resolved_abilities

    resolved = engine.select_elimination_target(row.id, 2)
    assert resolved.accepted
    assert target.eliminated
    assert killua_ability_id in killua.resolved_abilities

    # A second revival preserves the successful one-time resolution and does not retrigger it.
    engine.elimination.eliminate_target(engine.state, killua, target)
    assert _arm_chopper_selection(engine, chopper_two, row, slot_index=3)
    revived_again = engine.select_elimination_target(row.id, 1)
    assert revived_again.accepted
    assert not killua.eliminated
    assert killua_ability_id in killua.resolved_abilities
    assert engine.state.pending_elimination is None
    assert not any(
        event.kind == "ability_activated"
        and event.data.get("owner_card_id") == killua.id
        for event in revived_again.events
    )
    for player in (p1, p2):
        view = engine.view_for(player.id)
        killua_view = view["rows"][0]["slots"][1]["card"]
        assert killua_view["eliminated"] is False
        assert killua_ability_id in killua_view["resolved_abilities"]


def test_zeno_first_in_empty_row_erases_next_opponent_without_reveal_or_selection():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    zeno = card(engine, ZENO, p1.id)
    target = card(engine, KAKASHI, p2.id)
    p1.hand = [zeno]
    p2.hand = [target]

    place(engine, zeno, 0, 0)
    result = place(engine, target, 0, 1)
    eliminations = [event for event in result.events if event.kind == "elimination"]
    assert len(eliminations) == 1
    event = eliminations[0]
    assert event.message == "Kakashi Hatake has been eliminated by Zeno."
    assert event.data["attacker_card_id"] == zeno.id
    assert event.data["target_card_id"] == target.id
    assert event.data["animate"] is False
    resolved = [item for item in result.events if item.kind == "ability_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["ability_id"] == "zeno_erase_from_existence_trap"
    assert resolved[0].data["sound"] == "assets/sounds/Abillity_Sfx/Ability_Resolution/Zeno.wav"
    assert resolved[0].data["target_card_id"] == target.id
    assert "zeno_erase_from_existence_trap" in zeno.resolved_abilities
    assert target.eliminated and target.current_points == 0
    assert target.resolved_abilities == []
    assert not target.revealed
    assert zeno.revealed
    assert engine.state.pending_elimination is None
    assert not any(item.kind == "elimination_failed" for item in result.events)
    assert not any(
        item.kind == "card_revealed" and item.data.get("card_id") == target.id
        for item in result.events
    )
    assert zeno.activated_abilities == ["zeno_erase_from_existence_trap"]

    # Both multiplayer views preserve the hidden identity while exposing the X state.
    for viewer in (p1, p2):
        view = engine.view_for(viewer.id)
        target_slot = view["rows"][0]["slots"][1]
        assert target_slot["card"] is None
        assert target_slot["eliminated"] is True
        remote_state = _state_from_view(view)
        assert remote_state.rows[0].slots[1].eliminated
        assert not remote_state.rows[0].slots[1].revealed


def test_zeno_placed_after_another_card_does_not_arm_trap():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    earlier = card(engine, KAKASHI, p1.id)
    zeno = card(engine, ZENO, p2.id)
    target = card(engine, GO, p1.id)
    p1.hand = [earlier, target]
    p2.hand = [zeno]

    place(engine, earlier, 0, 0)
    place(engine, zeno, 0, 1)
    result = place(engine, target, 0, 2)
    assert not target.eliminated
    assert zeno.activated_abilities == []
    assert not any(event.kind == "elimination" for event in result.events)
    assert not any(event.kind == "ability_resolved" for event in result.events)


def test_zeno_ignores_friendly_placement_then_eliminates_next_opponent():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    zeno = card(engine, ZENO, p1.id)
    friendly = card(engine, KAKASHI, p1.id)
    opponent_elsewhere = card(engine, GO, p2.id)
    target = card(engine, HISOKA, p2.id)
    p1.hand = [zeno, friendly]
    p2.hand = [opponent_elsewhere, target]

    place(engine, zeno, 0, 0)
    place(engine, opponent_elsewhere, 1, 0)
    friendly_result = place(engine, friendly, 0, 1)
    assert not friendly.eliminated
    assert zeno.activated_abilities == []
    assert not any(event.kind == "elimination" for event in friendly_result.events)
    assert not any(event.kind == "ability_resolved" for event in friendly_result.events)

    result = place(engine, target, 0, 2)
    assert target.eliminated and target.current_points == 0
    assert not target.revealed
    assert len([event for event in result.events if event.kind == "elimination"]) == 1
    assert zeno.activated_abilities == ["zeno_erase_from_existence_trap"]


def test_zeno_trap_is_consumed_once_and_hidden_target_does_not_block_scoring():
    engine = scripted_engine(capacity=3)
    p1, p2 = engine.state.players
    zeno = card(engine, ZENO, p1.id)
    target = card(engine, KAKASHI, p2.id)
    p1.hand = [zeno]
    p2.hand = [target]
    place(engine, zeno, 0, 0)
    result = place(engine, target, 0, 1)
    elimination_count = sum(event.kind == "elimination" for event in engine.state.events)

    row = engine.state.rows[0]
    assert engine._resolve_zeno_trap(row, target, 1) is False
    assert sum(event.kind == "elimination" for event in engine.state.events) == elimination_count == 1

    # Fill the board and finish ordinary reveals. Eliminated hidden cards are
    # resolved for progression but remain face-down and contribute no points.
    while engine.state.phase == GamePhase.PLACEMENT:
        resolve_eliminations(engine)
        active = engine.state.active_player
        row_id, slot_index = engine.legal_slots(active.id)[0]
        filler = active.hand[0]
        placed = engine.play_card(active.id, filler.id, row_id, slot_index)
        assert placed.accepted, placed.reason
    while engine.state.phase == GamePhase.REVEAL:
        resolve_eliminations(engine)
        if engine.state.phase != GamePhase.REVEAL:
            break
        result = engine.reveal_next_card()
        assert result.accepted, result.reason
    assert engine.state.phase == GamePhase.ROW_SCORING
    assert not target.revealed
    assert engine.score_current_row().accepted
    assert row.scores[p2.id] == sum(
        card.current_points
        for card in row.slots
        if card is not None and card.owner_id == p2.id and card.revealed
    )
    assert target.current_points == 0


def test_zeno_elimination_announcement_dedupes_and_skips_flip_animation():
    renderer = Renderer.__new__(Renderer)
    renderer._processed_elimination_events = set()
    renderer.elimination_announcement = None
    renderer.elimination_announced_at = 0.0
    renderer.elimination_flash_card = None
    renderer.elimination_flash_started = None
    event = GameEvent("elimination", "Kakashi Hatake has been eliminated by Zeno.", {
        "attacker_name": "Zeno", "target_name": "Kakashi Hatake",
        "row_id": "row-1", "slot_index": 1,
        "elimination_id": "zeno-card:kakashi-card", "animate": False,
    })

    renderer.note_events((event,))
    announcement_time = renderer.elimination_announced_at
    renderer.note_events((event,))
    assert renderer.elimination_announcement == "KAKASHI HATAKE HAS BEEN ELIMINATED BY ZENO"
    assert renderer.elimination_announced_at == announcement_time
    assert renderer.elimination_flash_card is None
    assert renderer.elimination_flash_started is None


def test_base_zero_death_uses_died_announcement_once():
    renderer = Renderer.__new__(Renderer)
    renderer._processed_elimination_events = set()
    renderer.elimination_announcement = None
    renderer.elimination_announced_at = 0.0
    renderer.elimination_flash_card = None
    renderer.elimination_flash_started = None
    event = GameEvent("death", "Naruto Uzumaki Died", {
        "target_name": "Naruto Uzumaki", "row_id": "row-1", "slot_index": 1,
        "elimination_id": "death:naruto-card", "animate": True,
    })

    renderer.note_events((event,))
    announcement_time = renderer.elimination_announced_at
    renderer.note_events((event,))

    assert renderer.elimination_announcement == "Naruto Uzumaki Died"
    assert renderer.elimination_announced_at == announcement_time
    assert renderer.elimination_flash_card == ("row-1", 1)
