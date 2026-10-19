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
from veilbound.client.network import _state_from_view
from veilbound.shared.config import GameConfig
from veilbound.shared.engine import GameEngine
from veilbound.shared.elimination import EliminationManager, EliminationTargeting
from veilbound.shared.models import CardInstance, GamePhase
from veilbound.server.rooms import Room


KILLUA = "hxh_02"
GO = "naruto_01"        # 10 / GOAT
KAKASHI = "naruto_05"   # 7  / Fighter
ITACHI = "naruto_03"    # 6  / Dynamic Duo
HISOKA = "hxh_04"       # 8  / Fighter
LUFFY = "onepiece_01"   # 10 / GOAT
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
    pending = engine.state.pending_elimination
    assert pending is not None
    assert pending["attacker_card_id"] == killua.id
    assert pending["attacker_owner_id"] == "player-1"
    # Row-major order: KAKASHI (row0/s1) first, then HISOKA (row1/s0).
    assert [(t["row_id"], t["slot_index"]) for t in pending["valid_targets"]] == [
        (engine.state.rows[0].id, 1),
        (engine.state.rows[1].id, 0),
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

    # Reveal flow resumes and finishes the round normally.
    while engine.state.phase == GamePhase.REVEAL:
        assert engine.reveal_next_card().accepted
    assert engine.state.phase == GamePhase.ROW_SCORING
