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

from conftest import resolve_eliminations
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
SANJI = "onepiece_03"
ZORO = "onepiece_02"
VEGETA = "dragonball_02"
GOKU = "dragonball_01"
ALLUKA = "hxh_03"
KILLUA = "hxh_02"
SPECIAL = {SASUKE, ITACHI, SANJI, ZORO, VEGETA, GOKU, ALLUKA, KILLUA}
ABILITY_ID = "sasuke_shared_row_bonus"

# Filler cards must never carry an on-reveal ability: random draw piles can
# otherwise leak extra activations into unrelated scenarios. Keep this in
# sync with the roster JSON abilities.
NARUTO = "naruto_01"
ABILITY_HOLDERS = {SASUKE, SANJI, VEGETA, ALLUKA, KILLUA, NARUTO}
NON_ABILITY_FILLERS = [
    "naruto_05", "naruto_06", "naruto_07", "naruto_08", "naruto_10",
    "onepiece_01", "hxh_01", "dragonball_03",
]

# New synergy abilities that replicate Sasuke's pattern exactly.
NEW_DUOS = [
    (SANJI, ZORO, "sanji_zoro_row_bonus"),
    (VEGETA, GOKU, "vegeta_goku_row_bonus"),
    (ALLUKA, KILLUA, "alluka_killua_row_bonus"),
]

_instance_serial = itertools.count(1)


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


def make_engine(capacity=2, seed=4):
    engine = GameEngine(
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
    # Scrub random pools so only scripted cards can carry duo synergies.
    # The roster now has several Dynamic Duo abilities; random draw piles
    # would otherwise leak unrelated activations into exact-count asserts.
    filler_ids = NON_ABILITY_FILLERS
    serial = 0

    def _filler(owner_id: str) -> CardInstance:
        nonlocal serial
        definition_id = filler_ids[serial % len(filler_ids)]
        serial += 1
        return instance(engine, definition_id, owner_id)

    for player in engine.state.players:
        player.draw_pile = [_filler(player.id) for _ in player.draw_pile]
        player.hand = [_filler(player.id) for _ in player.hand]
    return engine


def pick_filler(player):
    card = next((c for c in player.hand if c.definition_id not in SPECIAL and c.definition_id != NARUTO), None)
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
        resolve_eliminations(engine)
        if engine.state.phase != GamePhase.PLACEMENT:
            break  # resolving an elimination closed the placement phase
        active = engine.state.active_player
        card = pick_filler(active)
        row_id, slot_index = engine.legal_slots()[0]
        result = engine.play_card(active.id, card.id, row_id, slot_index)
        assert result.accepted, result.reason
        results.append(result)
    resolve_eliminations(engine)
    return results


def finish_reveals(engine):
    """Run the final reveal sequence; returns one result per revealed card."""
    results = []
    while engine.state.phase == GamePhase.REVEAL:
        resolve_eliminations(engine)
        if engine.state.phase != GamePhase.REVEAL:
            # Resolving the final reveal's elimination completed the pass.
            break
        result = engine.reveal_next_card()
        assert result.accepted, result.reason
        results.append(result)
    resolve_eliminations(engine)
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

    # Different rows -> nothing happens, ever (same-player synergy requires
    # both partners to share the row *and* the owner).
    assert ability_events(placements) == []
    assert ability_events(reveals) == []
    assert sasuke.current_points == 7
    assert itachi.current_points == 6
    assert sasuke.activated_abilities == []
    # No stale registration survives: a pending Sasuke whose revealed partner
    # sits in another row can never become eligible, so the engine prunes it.
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
    # Same-player synergy: Sasuke + Itachi both belong to player 1 and share
    # row 0, so exactly one activation flows through the animation pipeline.
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, ITACHI, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    renderer = RecordingRenderer()
    controller = InputController(engine, renderer)

    # Mirror the app pipeline: every action result feeds renderer + audio.
    # After Sasuke (hand[0]) is placed, Itachi shifts to hand[0]; placing it at
    # row0/slot1 shares the row with Sasuke and reveals the already-placed card.
    controller._apply(place(engine, p1.hand[0], 0, 0), 0.0)
    controller._apply(place(engine, pick_filler(p2), 1, 0), 0.0)
    controller._apply(place(engine, p1.hand[0], 0, 1), 0.0)  # Itachi reveals Sasuke
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
    # Same-player synergy: each Sasuke activates off its OWN player's Itachi
    # sharing its row. Capacity 4 lets both duos share row 0 while keeping
    # turn order (p1, p2 alternate).
    engine = make_engine(capacity=4)
    p1, p2 = engine.state.players
    give(p1, engine, SASUKE, ITACHI, "naruto_01", "naruto_05")
    give(p2, engine, SASUKE, ITACHI, "naruto_06", "naruto_07")
    sasuke_a, itachi_a = p1.hand[0], p1.hand[1]
    sasuke_b, itachi_b = p2.hand[0], p2.hand[1]
    place(engine, sasuke_a, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    place(engine, itachi_a, 0, 1)  # reveals A (p1 Itachi shares row 0)
    place(engine, pick_filler(p2), 1, 1)
    place(engine, sasuke_b, 0, 2)
    place(engine, pick_filler(p2), 1, 2)
    place(engine, itachi_b, 0, 3)  # row 0 full -> final reveals pending
    place(engine, pick_filler(p2), 1, 3)
    fill_rest(engine)
    reveals = finish_reveals(engine)
    # Row-major finals: row-0 Itachi A reveals first -> Sasuke A fires
    # (same row, same owner p1); then Itachi B -> Sasuke B fires (p2).
    first_grant = ability_events(reveals[0])
    assert len(first_grant) == 1
    assert first_grant[0].data["owner_card_id"] == sasuke_a.id
    assert sasuke_a.current_points == 10
    assert sasuke_b.current_points == 7  # no shared state between instances
    assert sasuke_b.activated_abilities == []
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
        # Fill the remaining slots with non-duo cards; turns alternate. A
        # mid-placement reveal (placing Itachi reveals Sasuke) pauses further
        # placement until both peers ack it, so drain any pending reveal first
        # -- the same contract `drive_match` in test_multiplayer follows.
        while engine.state.phase == GamePhase.PLACEMENT:
            while room.pending_reveal_id is not None:
                reveal_id = room.pending_reveal_id
                for player_id in ("player-1", "player-2"):
                    await room.handle(room.peers[player_id], {
                        "type": "reveal_ack", "reveal_id": reveal_id,
                    })
            if engine.state.phase != GamePhase.PLACEMENT:
                break
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


def build_duo_same_owner(holder, partner):
    """Place holder + partner (both player 1) into row 0, then finish all reveals.

    The holder is revealed when the partner is placed; the partner is revealed
    during the final reveal pass, which triggers the +3 synergy. Returns
    (engine, holder_card, partner_card).
    """
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, holder, partner, "naruto_01", "naruto_05")
    give(p2, engine, "naruto_06", "naruto_07", "naruto_08", "naruto_10")
    holder_card, partner_card = p1.hand[0], p1.hand[1]
    place(engine, holder_card, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    place(engine, partner_card, 0, 1)  # reveals holder; partner still face-down
    place(engine, pick_filler(p2), 1, 1)
    fill_rest(engine)
    finish_reveals(engine)
    return engine, holder_card, partner_card


def test_new_synergy_partner_same_row_grants_plus_three():
    for holder, partner, ability_id in NEW_DUOS:
        engine, holder_card, partner_card = build_duo_same_owner(holder, partner)
        base = engine.cards[holder].base_points
        assert holder_card.current_points == base + 3, holder
        assert holder_card.activated_abilities == [ability_id]
        # The partner itself gains nothing from this ability.
        assert partner_card.current_points == engine.cards[partner].base_points


def test_new_synergy_opponent_partner_does_not_activate():
    for holder, partner, ability_id in NEW_DUOS:
        engine = make_engine()
        p1, p2 = engine.state.players
        give(p1, engine, holder, "naruto_01", "naruto_05", "naruto_08")
        give(p2, engine, partner, "naruto_06", "naruto_07", "naruto_10")
        holder_card, partner_card = p1.hand[0], p2.hand[0]
        place(engine, holder_card, 0, 0)
        place(engine, partner_card, 0, 1)  # opponent's partner shares the row
        fill_rest(engine)
        finish_reveals(engine)
        base = engine.cards[holder].base_points
        assert holder_card.activated_abilities == []
        if holder_card.eliminated:
            # Killua's Godspeed legitimately removed the holder; the synergy
            # itself never activated (nothing was recorded on the holder).
            assert holder_card.current_points == 0
        else:
            assert holder_card.current_points == base, holder


def test_new_synergy_repeated_evaluation_never_stacks():
    for holder, partner, ability_id in NEW_DUOS:
        engine, holder_card, partner_card = build_duo_same_owner(holder, partner)
        base = engine.cards[holder].base_points
        row0 = engine.state.rows[0]
        for card in (partner_card, holder_card, partner_card, holder_card):
            engine.abilities.on_card_revealed(engine.state, card, row0, 0)
        engine.score_current_row()
        assert holder_card.current_points == base + 3
        assert holder_card.activated_abilities == [ability_id]


def _retraction_bonus_after_partner_removed(holder, partner):
    engine, holder_card, partner_card = build_duo_same_owner(holder, partner)
    base = engine.cards[holder].base_points
    assert holder_card.current_points == base + 3
    row0 = engine.state.rows[0]
    idx = next(i for i, s in enumerate(row0.slots) if s is not None and s.id == partner_card.id)
    row0.slots[idx] = None  # artificial; the real game has no removal mechanic
    engine.abilities.on_card_revealed(engine.state, holder_card, row0, 0)
    engine.score_current_row()
    return holder_card.current_points - base  # bonus still applied (3) or removed (0)


def test_new_synergy_behaves_identically_to_sasuke_when_partner_removed():
    """The shared ability system applies bonuses once and does not retract them.

    The game has no card-removal/movement mechanic, so "partner leaves the row"
    cannot occur during real play. This asserts the new abilities are CONSISTENT
    with Sasuke (identical behaviour in this scenario) -- the acceptance criterion
    -- rather than asserting that retraction occurs.
    """
    sasuke_bonus = _retraction_bonus_after_partner_removed(SASUKE, ITACHI)
    for holder, partner, _ability_id in NEW_DUOS:
        assert _retraction_bonus_after_partner_removed(holder, partner) == sasuke_bonus, holder

def test_naruto_hinata_partner_same_row_grants_plus_three():
    engine, holder_card, partner_card = build_duo_same_owner(NARUTO, "naruto_10")
    base = engine.cards[NARUTO].base_points
    assert holder_card.current_points == base + 3
    assert "naruto_hinata_bond_of_love" in holder_card.activated_abilities
    # The partner itself gains nothing from this ability.
    assert partner_card.current_points == engine.cards["naruto_10"].base_points


def test_naruto_hinata_opponent_partner_does_not_activate():
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, NARUTO, "naruto_01", "naruto_05", "naruto_08")
    give(p2, engine, "naruto_10", "naruto_06", "naruto_07", "naruto_08")
    holder_card, partner_card = p1.hand[0], p2.hand[0]
    place(engine, holder_card, 0, 0)
    place(engine, partner_card, 0, 1)  # opponent's Hinata shares the row
    place(engine, pick_filler(p1), 1, 0)
    place(engine, pick_filler(p2), 1, 1)
    fill_rest(engine)
    finish_reveals(engine)
    base = engine.cards[NARUTO].base_points
    assert holder_card.current_points == base
    assert holder_card.activated_abilities == []


def test_naruto_hinata_no_bonus_different_rows():
    engine = make_engine()
    p1, p2 = engine.state.players
    give(p1, engine, NARUTO, "naruto_05", "naruto_06", "naruto_07")
    give(p2, engine, "naruto_10", "naruto_06", "naruto_07", "naruto_08")
    holder_card, partner_card = p1.hand[0], p1.hand[1]
    place(engine, holder_card, 0, 0)
    place(engine, pick_filler(p2), 1, 0)
    place(engine, partner_card, 1, 1)  # different row
    fill_rest(engine)
    finish_reveals(engine)
    base = engine.cards[NARUTO].base_points
    assert holder_card.current_points == base
    assert holder_card.activated_abilities == []


def test_naruto_hinata_bonus_removed_when_partner_eliminated():
    engine, holder_card, partner_card = build_duo_same_owner(NARUTO, "naruto_10")
    base = engine.cards[NARUTO].base_points
    assert holder_card.current_points == base + 3
    # Simulate partner elimination: mark the card eliminated and recalc continuous bonuses
    partner_slot = next(s for s in engine.state.rows[0].slots if s is not None and s.id == partner_card.id)
    partner_slot.eliminated = True
    partner_slot.current_points = 0
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert holder_card.current_points == base
    assert partner_slot.eliminated


def test_naruto_hinata_bonus_restored_when_partner_reactivated():
    engine, holder_card, partner_card = build_duo_same_owner(NARUTO, "naruto_10")
    base = engine.cards[NARUTO].base_points
    assert holder_card.current_points == base + 3
    # Eliminate partner
    partner_slot = next(s for s in engine.state.rows[0].slots if s is not None and s.id == partner_card.id)
    partner_slot.eliminated = True
    partner_slot.current_points = 0
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert holder_card.current_points == base
    # Reactivate partner (simulate re-placement)
    partner_slot.eliminated = False
    partner_slot.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert holder_card.current_points == base + 3
    # Ensure no stacking
    assert holder_card.current_points == base + 3


def test_naruto_hinata_repeated_recalc_never_stacks():
    engine, holder_card, partner_card = build_duo_same_owner(NARUTO, "naruto_10")
    base = engine.cards[NARUTO].base_points
    # Activate the bonus
    assert holder_card.current_points == base + 3
    # Recalc multiple times: should stay at +3
    for _ in range(5):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
        assert holder_card.current_points == base + 3, holder_card.current_points


def test_naruto_elimination_ability_definition():
    engine = make_engine()
    # The first ability on Naruto should be the elimination ability
    elimination_ability = engine.cards[NARUTO].abilities[0]
    assert elimination_ability.id == "naruto_rasenshuriken_eliminate"
    assert elimination_ability.trigger == "on_reveal"
    assert [e.type for e in elimination_ability.effects] == ["eliminate_target"]

