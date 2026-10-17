"""Phase-1 ability tests: Sasuke's Shared Row Bonus and the ability manager contract.

Covers the required scenarios: both reveal orders, different rows, hidden
partners, repeated evaluation, reveal-animation flow, base/current separation,
match reset, per-instance isolation, scoring integration, deterministic
ordering of simultaneous abilities, validation errors, floating-text
presentation, and multiplayer single-resolution sync.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import itertools
import time
from types import SimpleNamespace

import pygame
import pytest

from test_input_animations import RecordingRenderer
from test_multiplayer import FakeWriter

from veilbound.client.input import InputController
from veilbound.client.network import _state_from_view
from veilbound.client.renderer import Renderer
from veilbound.server.rooms import Room
from veilbound.shared.abilities import (
    AbilityDefinition,
    AbilityValidationError,
    Condition,
    parse_ability,
    validate_ability_references,
)
from veilbound.shared.config import GameConfig
from veilbound.shared.definitions import load_card_definitions
from veilbound.shared.engine import GameEngine
from veilbound.shared.models import CardInstance, GameEvent, GamePhase
from veilbound.shared.scoring import calculate_row_scores

SASUKE = "naruto_02"
ITACHI = "naruto_03"
SPECIAL = {SASUKE, ITACHI}
ABILITY_ID = "sasuke_shared_row_bonus"

_instance_serial = itertools.count(1)


def make_engine(capacity=2, seed=4):
    return GameEngine(
        GameConfig(
            deck_size=40,
            initial_hand_size=5,
            draw_per_turn=1,
            capacity_min=capacity,
            capacity_max=capacity,
            rounds_per_match=2,
        ),
        seed=seed,
    )


def instance(engine, definition_id, owner_id):
    definition = engine.cards[definition_id]
    return CardInstance(
        f"test-card-{next(_instance_serial)}",
        definition_id,
        owner_id,
        current_points=definition.base_points,
    )


def give(player, engine, *definition_ids):
    """Replace a hand with a scripted card list for a deterministic scenario."""
    player.hand = [instance(engine, did, player.id) for did in definition_ids]


def pick_filler(player):
    card = next((c for c in player.hand if c.definition_id not in SPECIAL), None)
    assert card is not None, "scenario needs a non-duo filler card in hand"
    return card


def place(engine, card, row_index, slot_index):
    active = engine.state.active_player
    assert active.id == card.owner_id, "test sequence must follow turn order"
    result = engine.play_card(active.id, card.id, engine.state.rows[row_index].id, slot_index)
    assert result.accepted, result.reason
    return result


def fill_rest(engine):
    """Finish the placement phase with filler cards (turns alternate)."""
    results = []
    while engine.state.phase == GamePhase.PLACEMENT:
        active = engine.state.active_player
        card = pick_filler(active)
        row_id, slot_index = engine.legal_slots()[0]
        result = engine.play_card(active.id, card.id, row_id, slot_index)
        assert result.accepted, result.reason
        results.append(result)
    return results


def finish_reveals(engine):
    """Run the final reveal sequence; returns one result per revealed card."""
    results = []
    while engine.state.phase == GamePhase.REVEAL:
        result = engine.reveal_next_card()
        assert result.accepted, result.reason
        results.append(result)
    assert engine.state.phase == GamePhase.ROW_SCORING
    return results


def ability_events(results):
    if hasattr(results, "events"):
        results = [results]
    return [
        event
        for result in results
        for event in result.events
        if event.kind == "ability_activated"
    ]


def build_sasuke_first():
    """Sasuke revealed while Itachi stays hidden in the same row.

    Returns the filled engine plus the duo instances; the bonus has not been
    granted yet (final reveals still pending).
    """
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, ITACHI, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    sasuke, itachi = p1.hand[0], p1.hand[1]
    place(engine, sasuke, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    placement = place(engine, itachi, 0, 1)  # reveals Sasuke only
    place(engine, pick_filler(p2), 1, 1)
    fill_rest(engine)
    return engine, sasuke, itachi, placement


def test_sasuke_revealed_first_gains_bonus_when_itachi_later_revealed_same_row():
    engine, sasuke, itachi, placement = build_sasuke_first()

    # Rules 3-4: Sasuke is revealed, Itachi is not -> registered, no award.
    assert sasuke.revealed and not itachi.revealed
    assert ability_events(placement) == []
    assert sasuke.current_points == 7
    assert engine.abilities.pending_abilities  # Phase A candidate exists

    reveals = finish_reveals(engine)
    # First final reveal is Itachi (row 1, slot 1): reveal first, then Phase A.
    assert [event.kind for event in reveals[0].events] == [
        "card_revealed",
        "ability_activated",
    ]
    granted = ability_events(reveals[0])
    assert len(granted) == 1
    data = granted[0].data
    assert data["ability_id"] == ABILITY_ID
    assert data["ability_name"] == "Shared Row Bonus"
    assert data["owner_card_id"] == sasuke.id
    assert data["target_card_id"] == sasuke.id  # targets Sasuke, not Itachi
    assert data["row_id"] == engine.state.rows[0].id
    assert data["slot_index"] == 0
    assert data["delta"] == 3
    assert data["base_points"] == 7
    assert data["current_points"] == 10
    assert data["floating_text"] == "+3"
    assert data["animation"] == "points_gain"
    assert sasuke.current_points == 10
    assert itachi.current_points == 6
    # Exactly one activation across every remaining reveal of the round.
    assert ability_events(reveals[1:]) == []
    assert len(ability_events(reveals)) == 1


def test_itachi_revealed_first_grants_bonus_when_sasuke_revealed_after():
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, ITACHI, SASUKE, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    itachi, sasuke = p1.hand[0], p1.hand[1]

    place(engine, itachi, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    placement = place(engine, sasuke, 0, 1)  # reveals Itachi only
    # Itachi carries no ability, so its reveal awards nothing (rule 7 order).
    assert ability_events(placement) == []
    place(engine, pick_filler(p2), 1, 1)
    fill_rest(engine)

    reveals = finish_reveals(engine)
    # Phase B: the bonus resolves on Sasuke's own reveal, same batch order.
    assert [event.kind for event in reveals[0].events] == [
        "card_revealed",
        "ability_activated",
    ]
    granted = ability_events(reveals[0])
    assert len(granted) == 1
    assert granted[0].data["owner_card_id"] == sasuke.id
    assert sasuke.current_points == 10
    assert itachi.current_points == 6  # bonus targets Sasuke only


def test_different_rows_never_activate_the_bonus():
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, "naruto_01", "naruto_05", "naruto_08")
    give(p2, engine, ITACHI, "naruto_06", "naruto_07", "naruto_10")
    sasuke, itachi = p1.hand[0], p2.hand[0]

    placements = [
        place(engine, sasuke, 0, 0),
        place(engine, itachi, 1, 0),
        place(engine, pick_filler(p1), 0, 1),  # reveals Sasuke
        place(engine, pick_filler(p2), 1, 1),  # reveals Itachi
    ]
    fill_rest(engine)
    reveals = finish_reveals(engine)

    # Rule 6: both revealed, different rows -> nothing happens, ever.
    assert ability_events(placements) == []
    assert ability_events(reveals) == []
    assert sasuke.current_points == 7
    assert itachi.current_points == 6
    assert sasuke.activated_abilities == []
    assert engine.abilities.pending_abilities == ()


def test_no_bonus_while_either_character_is_hidden():
    # Case A: Sasuke revealed first, Itachi stays hidden this whole time.
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, ITACHI, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    sasuke, itachi = p1.hand[0], p1.hand[1]
    place(engine, sasuke, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    placement = place(engine, itachi, 0, 1)  # reveals Sasuke; Itachi hidden
    assert ability_events(placement) == []
    assert sasuke.current_points == 7
    assert not itachi.revealed
    assert engine.abilities.pending_abilities  # waiting, not awarded

    # Case B (fresh match): Itachi revealed first while Sasuke stays hidden.
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, ITACHI, SASUKE, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    itachi, sasuke = p1.hand[0], p1.hand[1]
    place(engine, itachi, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    placement = place(engine, sasuke, 0, 1)  # reveals Itachi; Sasuke hidden
    assert ability_events(placement) == []
    assert not sasuke.revealed
    assert sasuke.current_points == 7
    # A hidden owner never registers; only revealed owners enter Phase A.
    assert engine.abilities.pending_abilities == ()


def test_repeated_evaluation_grants_the_bonus_only_once():
    engine, sasuke, itachi, _ = build_sasuke_first()
    reveals = finish_reveals(engine)
    assert sasuke.current_points == 10
    assert len(ability_events(reveals)) == 1

    # Re-run the manager over the finished board many times (rule 9/10).
    row, index = engine.state.rows[0], 1
    for card in (itachi, sasuke, itachi, sasuke):
        again = engine.abilities.on_card_revealed(engine.state, card, row, index)
        assert again == []
    assert sasuke.current_points == 10

    # Scoring never re-grants; duplicate event delivery is renderer-guarded.
    score = engine.score_current_row()
    assert ability_events(score) == []
    assert sasuke.current_points == 10
    assert sasuke.activated_abilities == [ABILITY_ID]


def test_reveal_animation_flow_runs_with_a_single_activation():
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, ITACHI, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    renderer = RecordingRenderer()
    controller = InputController(engine, renderer)

    # Mirror the app pipeline: every action result feeds renderer + audio.
    controller._apply(place(engine, p1.hand[0], 0, 0), 0.0)
    controller._apply(place(engine, pick_filler(p2), 1, 0), 0.0)
    controller._apply(place(engine, p1.hand[1], 0, 1), 0.0)  # reveals Sasuke
    assert controller.animation_stage == "card_flip"
    controller._apply(place(engine, pick_filler(p2), 1, 1), 0.0)
    for result in fill_rest(engine):
        controller._apply(result, 0.0)

    # Drive final reveals and scoring through the real deadline loop.
    now = 0.0
    for _ in range(400):
        controller.update(now)
        now += 0.1
        if engine.state.phase in {
            GamePhase.ROW_RESULTS,
            GamePhase.ROUND_RESULTS,
            GamePhase.MATCH_RESULTS,
        }:
            break

    kinds = [event.kind for event in renderer.events]
    assert engine.state.phase in {
        GamePhase.ROW_RESULTS,
        GamePhase.ROUND_RESULTS,
        GamePhase.MATCH_RESULTS,
    }
    # Every card flipped exactly once through the animation machinery...
    assert kinds.count("card_revealed") == 8
    # ...while the ability activated exactly once overall (no frame duplicates).
    assert kinds.count("ability_activated") == 1


def test_base_points_remain_unchanged_after_the_bonus():
    engine, sasuke, _, _ = build_sasuke_first()
    finish_reveals(engine)
    assert sasuke.current_points == 10
    assert engine.cards[SASUKE].base_points == 7
    # Reloading the roster JSON is untouched as well.
    fresh = load_card_definitions()
    assert fresh[SASUKE].base_points == 7
    assert fresh[SASUKE].ability is not None
    assert fresh[SASUKE].ability.id == ABILITY_ID


def test_new_match_resets_current_points_and_activation_records():
    engine, sasuke, _, _ = build_sasuke_first()
    finish_reveals(engine)
    assert sasuke.current_points == 10
    assert sasuke.activated_abilities == [ABILITY_ID]

    engine.start_new_match()
    assert engine.abilities.pending_abilities == ()
    for player in engine.state.players:
        for card in player.draw_pile + player.hand + player.used:
            assert card.current_points == engine.cards[card.definition_id].base_points
            assert card.activated_abilities == []


def test_two_sasuke_instances_keep_independent_points_and_activations():
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, ITACHI, "naruto_01", "naruto_05")
    give(p2, engine, SASUKE, ITACHI, "naruto_06", "naruto_07")
    sasuke_a, itachi_a = p1.hand[0], p1.hand[1]
    sasuke_b, itachi_b = p2.hand[0], p2.hand[1]

    place(engine, sasuke_a, 0, 0)
    place(engine, sasuke_b, 1, 0)
    place(engine, itachi_a, 0, 1)  # reveals Sasuke A -> pending
    place(engine, itachi_b, 1, 1)  # reveals Sasuke B -> pending
    fill_rest(engine)
    reveals = finish_reveals(engine)

    # First final reveal: Itachi A -> only instance A is eligible right now.
    first_grant = ability_events(reveals[0])
    assert len(first_grant) == 1
    assert first_grant[0].data["owner_card_id"] == sasuke_a.id
    assert sasuke_a.current_points == 10
    assert sasuke_b.current_points == 7  # no shared state between instances
    assert sasuke_b.activated_abilities == []

    # Second final reveal: Itachi B -> instance B activates separately.
    second_grant = ability_events(reveals[1])
    assert len(second_grant) == 1
    assert second_grant[0].data["owner_card_id"] == sasuke_b.id
    assert sasuke_a.current_points == 10
    assert sasuke_b.current_points == 10
    assert sasuke_a.activated_abilities == [ABILITY_ID]
    assert sasuke_b.activated_abilities == [ABILITY_ID]
    assert ability_events(reveals[2:]) == []


def test_itachi_declares_no_ability_and_contributes_base_points():
    engine = make_engine()
    assert engine.cards[ITACHI].ability is None

    engine, sasuke, itachi, _ = build_sasuke_first()
    finish_reveals(engine)
    assert itachi.current_points == 6 == engine.cards[ITACHI].base_points
    assert itachi.activated_abilities == []
    # The only activation in the round belongs to Sasuke.
    owners = [
        event.data["owner_card_id"]
        for event in engine.state.events
        if event.kind == "ability_activated"
    ]
    assert owners == [sasuke.id]


def test_scoring_uses_sasuke_modified_current_points():
    engine, sasuke, itachi, _ = build_sasuke_first()
    finish_reveals(engine)
    p1, p2 = engine.state.players
    row = engine.state.rows[0]

    # Row totals (live and scored) use current points: 10 + 6 for player 1.
    assert calculate_row_scores(row, (p1.id, p2.id), engine.cards) == {
        p1.id: 16,
        p2.id: 0,
    }
    score = engine.score_current_row()
    assert score.accepted
    assert row.scores == {p1.id: 16, p2.id: 0}
    assert row.winner_id == p1.id
    assert row.awarded_points == row.point_value
    assert p1.match_points == row.point_value


def test_simultaneous_eligible_abilities_resolve_in_registration_order():
    engine = make_engine(capacity=4)
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, "naruto_05", SASUKE, ITACHI)
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10", "naruto_01")
    sasuke_a, sasuke_b, itachi = p1.hand[0], p1.hand[2], p1.hand[3]

    place(engine, sasuke_a, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    place(engine, sasuke_b, 0, 1)  # reveals A -> registers A (sequence 1)
    place(engine, pick_filler(p2), 1, 1)
    place(engine, itachi, 0, 2)  # reveals B -> registers B (sequence 2)
    # Documented order: registration sequence (= owner reveal order).
    assert engine.abilities.pending_abilities == (
        (1, ABILITY_ID, sasuke_a.id),
        (2, ABILITY_ID, sasuke_b.id),
    )
    place(engine, pick_filler(p2), 1, 2)

    # Placing a filler into row 1 slot 4 reveals Itachi: both pending
    # abilities become eligible in the same pass.
    reveal_itachi = place(engine, pick_filler(p1), 0, 3)
    kinds = [
        event.kind
        for event in reveal_itachi.events
        if event.kind in {"card_revealed", "ability_activated"}
    ]
    assert kinds == ["card_revealed", "ability_activated", "ability_activated"]
    owners = [
        event.data["owner_card_id"]
        for event in reveal_itachi.events
        if event.kind == "ability_activated"
    ]
    assert owners == [sasuke_a.id, sasuke_b.id]
    assert sasuke_a.current_points == 10
    assert sasuke_b.current_points == 10
    assert engine.abilities.pending_abilities == ()


def test_multiplayer_room_resolves_the_bonus_once_and_syncs_both_peers():
    room = Room("SASUKE")
    writers = {"player-1": FakeWriter(), "player-2": FakeWriter()}
    asyncio.run(room.attach("player-1", writers["player-1"]))
    asyncio.run(room.attach("player-2", writers["player-2"]))

    engine = room.engine
    p1, p2 = engine.state.players
    sasuke = instance(engine, SASUKE, p1.id)
    itachi = instance(engine, ITACHI, p1.id)
    p1.hand.append(sasuke)
    p1.hand.append(itachi)

    async def play_to_reveal():
        rows = engine.state.rows
        await room.handle(room.peers["player-1"], {
            "type": "place", "card_id": sasuke.id,
            "row_id": rows[0].id, "slot_index": 0,
        })
        p2_filler = pick_filler(p2)
        await room.handle(room.peers["player-2"], {
            "type": "place", "card_id": p2_filler.id,
            "row_id": rows[1].id, "slot_index": 0,
        })
        await room.handle(room.peers["player-1"], {
            "type": "place", "card_id": itachi.id,
            "row_id": rows[0].id, "slot_index": 1,
        })
        # Fill the remaining slots with non-duo cards; turns alternate.
        while engine.state.phase == GamePhase.PLACEMENT:
            active = engine.state.active_player
            card = pick_filler(active)
            row_id, slot_index = engine.legal_slots()[0]
            await room.handle(room.peers[active.id], {
                "type": "place", "card_id": card.id,
                "row_id": row_id, "slot_index": slot_index,
            })

    async def ack_reveals():
        while room.pending_reveal_id is not None:
            reveal_id = room.pending_reveal_id
            for player_id in ("player-1", "player-2"):
                await room.handle(room.peers[player_id], {
                    "type": "reveal_ack", "reveal_id": reveal_id,
                })

    asyncio.run(play_to_reveal())
    asyncio.run(ack_reveals())
    assert engine.state.phase == GamePhase.ROW_SCORING

    def events_of(writer):
        return [event for msg in writer.updates() for event in msg.get("events", [])]

    # Both clients saw the activation exactly once, with identical payloads.
    for player_id, writer in writers.items():
        grants = [ev for ev in events_of(writer) if ev["kind"] == "ability_activated"]
        assert len(grants) == 1, f"{player_id} saw {len(grants)} activations"
        assert grants[0]["data"]["current_points"] == 10
        assert grants[0]["data"]["delta"] == 3
        assert grants[0]["data"]["floating_text"] == "+3"

        view = writer.updates()[-1]["view"]
        duo = [
            slot["card"]
            for row in view["rows"]
            for slot in row["slots"]
            if slot.get("card") and slot["card"]["definition_id"] == SASUKE
        ]
        assert len(duo) == 1
        assert duo[0]["current_points"] == 10
        assert duo[0]["activated_abilities"] == [ABILITY_ID]

        # A client state rebuilt from the view keeps the modified points.
        rebuilt = _state_from_view(view)
        rebuilt_card = next(
            slot
            for row in rebuilt.rows
            for slot in row.slots
            if slot is not None and slot.revealed and slot.definition_id == SASUKE
        )
        assert rebuilt_card.current_points == 10
        assert rebuilt_card.activated_abilities == [ABILITY_ID]

    # The authority itself resolved exactly once (no double award).
    assert sasuke.current_points == 10
    assert sasuke.activated_abilities == [ABILITY_ID]
    assert itachi.current_points == 6
