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
STAR_AND_STRIPES = "mha_10"
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
        "ability_resolved",
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
    resolved = [event for event in reveals[0].events if event.kind == "ability_resolved"]
    assert len(resolved) == 1
    assert resolved[0].data["ability_id"] == ABILITY_ID
    assert resolved[0].data["owner_card_id"] == sasuke.id
    assert resolved[0].data["target_card_id"] == sasuke.id
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
        "ability_resolved",
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


def test_itachi_declares_tsukuyomi_bonus_removal_target_ability():
    engine = make_engine()
    ability = engine.cards[ITACHI].ability
    assert ability is not None
    assert ability.id == "itachi_tsukuyomi_remove_bonus"
    assert ability.trigger == "on_reveal"
    assert [effect.type for effect in ability.effects] == ["remove_target_bonus"]
    assert ability.target_scope == "same_row"
    assert ability.target_side == "opposing"


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


def _sakura_bonus_board():
    engine = make_engine(capacity=4)
    p1, p2 = engine.state.players
    sakura = instance(engine, "naruto_04", p1.id)
    row = engine.state.rows[0]
    row.slots[0] = sakura
    sakura.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    return engine, sakura


def test_sakura_or_row_bonus_is_friendly_revealed_dynamic_and_does_not_stack():
    engine, sakura = _sakura_bonus_board()
    p1, p2 = engine.state.players
    base = engine.cards["naruto_04"].base_points
    assert sakura.current_points == base
    assert sakura.base_points is None  # instance keeps using the unchanged roster base
    assert engine.cards["naruto_04"].base_points == base

    naruto = instance(engine, "naruto_01", p1.id)
    engine.state.rows[0].slots[1] = naruto
    naruto.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sakura.current_points == base + 2

    sasuke = instance(engine, "naruto_02", p1.id)
    engine.state.rows[0].slots[2] = sasuke
    sasuke.revealed = True
    for _ in range(5):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sakura.current_points == base + 2  # both partners still grant one bonus

    naruto.eliminated = True
    naruto.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, naruto)
    assert sakura.current_points == base + 2  # Sasuke still qualifies

    sasuke.eliminated = True
    sasuke.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, sasuke)
    assert sakura.current_points == base

    # Reactivating a qualifying ally restores the same keyed contribution.
    sasuke.eliminated = False
    sasuke.current_points = engine.cards["naruto_02"].base_points
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sakura.current_points == base + 2
    assert sakura.base_points is None

    # Both multiplayer projections expose the same authoritative point total.
    for player in engine.state.players:
        view = engine.view_for(player.id)
        card_view = next(
            slot["card"] for row_view in view["rows"] for slot in row_view["slots"]
            if slot is not None and slot["card"]["id"] == sakura.id
        )
        assert card_view["current_points"] == base + 2


def test_sakura_or_row_bonus_rejects_opposing_other_row_and_eliminated_partners():
    engine, sakura = _sakura_bonus_board()
    p1, p2 = engine.state.players
    base = engine.cards["naruto_04"].base_points

    opposing_naruto = instance(engine, "naruto_01", p2.id)
    engine.state.rows[0].slots[1] = opposing_naruto
    opposing_naruto.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sakura.current_points == base

    other_row_sasuke = instance(engine, "naruto_02", p1.id)
    engine.state.rows[1].slots[0] = other_row_sasuke
    other_row_sasuke.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sakura.current_points == base

    opposing_naruto.owner_id = p1.id
    opposing_naruto.eliminated = True
    opposing_naruto.current_points = 0
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert sakura.current_points == base


def _rock_lee_board():
    engine = make_engine(capacity=4)
    p1 = engine.state.players[0]
    rock = instance(engine, "naruto_09", p1.id)
    engine.state.rows[0].slots[0] = rock
    rock.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    return engine, rock


def test_rock_lee_lone_warrior_tracks_active_friendly_cards_without_stacking():
    engine, rock = _rock_lee_board()
    p1, p2 = engine.state.players
    base = engine.cards["naruto_09"].base_points
    assert rock.current_points == base + 4
    assert rock.base_points is None
    # Keep a separate, valid ability contribution to verify Rock Lee's
    # continuous bonus can be removed without erasing unrelated points.
    rock.applied_bonuses = {
        "rock_lee_lone_warrior": 4,
        "naruto_rasenshuriken_eliminate:other-card": 3,
    }
    rock.current_points = base + 7

    # Opposing cards in his row and friendly cards in another row do not count.
    opponent = instance(engine, "naruto_05", p2.id)
    other_row_friend = instance(engine, "naruto_08", p1.id)
    engine.state.rows[0].slots[1] = opponent
    engine.state.rows[1].slots[0] = other_row_friend
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert rock.current_points == base + 7

    # A placed friendly card counts even while still face down; a second one
    # also keeps the lone-warrior condition false.
    first_friend = instance(engine, "naruto_08", p1.id)
    second_friend = instance(engine, "naruto_06", p1.id)
    engine.state.rows[0].slots[2] = first_friend
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert rock.current_points == base + 3
    engine.state.rows[0].slots[3] = second_friend
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert rock.current_points == base + 3

    # Removing one friend does not restore the bonus while another remains.
    first_friend.eliminated = True
    first_friend.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, first_friend)
    assert rock.current_points == base + 3

    # Eliminating the last active friendly card restores +4 immediately.
    second_friend.eliminated = True
    second_friend.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, second_friend)
    for _ in range(4):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert rock.current_points == base + 7
    assert engine.cards["naruto_09"].base_points == base

    for player in engine.state.players:
        view = engine.view_for(player.id)
        rock_view = next(
            slot["card"] for row_view in view["rows"] for slot in row_view["slots"]
            if slot is not None and slot["card"]["id"] == rock.id
        )
        assert rock_view["current_points"] == base + 7


def _hinata_bonus_board():
    engine = make_engine(capacity=5)
    p1 = engine.state.players[0]
    hinata = instance(engine, "naruto_10", p1.id)
    engine.state.rows[0].slots[0] = hinata
    hinata.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    return engine, hinata


def test_hinata_regular_synergy_scales_and_recalculates_with_active_friendly_cards():
    engine, hinata = _hinata_bonus_board()
    p1, p2 = engine.state.players
    base = engine.cards["naruto_10"].base_points
    ability_id = "hinata_byakugan_regular_synergy"
    assert hinata.current_points == base
    assert ability_id not in hinata.applied_bonuses

    # Preserve an independent bonus while Hinata's calculated amount changes.
    unrelated_key = "naruto_rasenshuriken_eliminate:other-card"
    hinata.applied_bonuses = {unrelated_key: 3}
    hinata.current_points = base + 3

    one_regular = instance(engine, "naruto_08", p1.id)
    engine.state.rows[0].slots[1] = one_regular
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 3  # unrevealed ally does not contribute
    assert ability_id not in hinata.applied_bonuses

    one_regular.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 4
    assert hinata.applied_bonuses[ability_id] == 1

    second_regular = instance(engine, "naruto_09", p1.id)
    engine.state.rows[0].slots[2] = second_regular
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 4
    second_regular.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 5
    assert hinata.applied_bonuses[ability_id] == 2

    third_regular = instance(engine, "naruto_08", p1.id)
    engine.state.rows[0].slots[3] = third_regular
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 5
    third_regular.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 6
    assert hinata.applied_bonuses[ability_id] == 3

    # Opposing Regular and friendly Regular in another row are not counted.
    opponent_regular = instance(engine, "naruto_10", p2.id)
    opponent_regular.revealed = True
    other_row_regular = instance(engine, "naruto_08", p1.id)
    other_row_regular.revealed = True
    engine.state.rows[0].slots[4] = opponent_regular
    engine.state.rows[1].slots[0] = other_row_regular
    for _ in range(4):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert hinata.current_points == base + 6
    assert hinata.applied_bonuses[ability_id] == 3
    assert hinata.applied_bonuses[unrelated_key] == 3

    # Hinata herself is Regular but never counts as a supporting card.
    assert sum(
        1 for card in engine.state.rows[0].slots
        if card is not None and card.owner_id == p1.id and not card.eliminated
        and engine.cards[card.definition_id].type == "Regular"
    ) == 4  # Hinata plus the three allies
    assert hinata.applied_bonuses[ability_id] == 3

    # The amount drops by one per eliminated ally, then reaches zero.
    one_regular.eliminated = True
    one_regular.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, one_regular)
    assert hinata.current_points == base + 5
    assert hinata.applied_bonuses[ability_id] == 2

    second_regular.eliminated = True
    second_regular.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, second_regular)
    assert hinata.current_points == base + 4
    assert hinata.applied_bonuses[ability_id] == 1

    third_regular.eliminated = True
    third_regular.current_points = 0
    engine.abilities.recalculate_after_elimination(engine.state, third_regular)
    assert hinata.current_points == base + 3
    assert ability_id not in hinata.applied_bonuses
    assert engine.cards["naruto_10"].base_points == base

    for player in engine.state.players:
        view = engine.view_for(player.id)
        hinata_view = next(
            slot["card"] for row_view in view["rows"] for slot in row_view["slots"]
            if slot is not None and slot["card"]["id"] == hinata.id
        )
        assert hinata_view["current_points"] == base + 3


def test_usopp_bonus_requires_a_revealed_opposing_goat_and_recalculates():
    engine = make_engine(capacity=5)
    p1, p2 = engine.state.players
    usopp = instance(engine, "onepiece_08", p1.id)
    friendly_goat = instance(engine, NARUTO, p1.id)
    hidden_opposing_goat = instance(engine, "onepiece_01", p2.id)
    other_opposing_goat = instance(engine, NARUTO, p2.id)
    row = engine.state.rows[0]
    usopp.revealed = True
    friendly_goat.revealed = True
    row.slots[:4] = [usopp, friendly_goat, hidden_opposing_goat, other_opposing_goat]
    base = engine.cards[usopp.definition_id].base_points
    ability_id = "usopp_god_opposing_goat_bonus"

    # Friendly GOATs and hidden opposing GOATs are ineligible.
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert usopp.current_points == base
    assert ability_id not in usopp.applied_bonuses

    engine._reveal_card(row, 2, hidden_opposing_goat)
    assert usopp.current_points == base + 3
    assert usopp.applied_bonuses[ability_id] == 3

    # Another revealed opposing GOAT keeps the same single +3 contribution.
    other_opposing_goat.revealed = True
    engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert usopp.current_points == base + 3

    # An independent, still-valid contribution remains when one or both GOATs leave.
    unrelated_key = "sakura_naruto_or_sasuke_row_bonus"
    usopp.applied_bonuses[unrelated_key] = 2
    usopp.current_points += 2
    engine.elimination.eliminate_target(engine.state, hidden_opposing_goat, usopp)
    engine.abilities.recalculate_after_elimination(engine.state, hidden_opposing_goat)
    assert usopp.current_points == base + 5
    assert usopp.applied_bonuses[ability_id] == 3
    assert usopp.applied_bonuses[unrelated_key] == 2

    engine.elimination.eliminate_target(engine.state, other_opposing_goat, usopp)
    engine.abilities.recalculate_after_elimination(engine.state, other_opposing_goat)
    assert usopp.current_points == base + 2
    assert ability_id not in usopp.applied_bonuses
    assert usopp.applied_bonuses[unrelated_key] == 2
    assert engine.cards[usopp.definition_id].base_points == base
    row_scores = calculate_row_scores(
        row, (p1.id, p2.id), engine.cards,
    )
    assert row_scores[p1.id] == usopp.current_points + friendly_goat.current_points
    assert row_scores[p2.id] == 0

    for player in engine.state.players:
        usopp_view = engine.view_for(player.id)["rows"][0]["slots"][0]["card"]
        assert usopp_view["base_points"] == base
        assert usopp_view["current_points"] == base + 2
        assert usopp_view["applied_bonuses"][unrelated_key] == 2


def test_star_and_stripes_bonus_tracks_latest_placement_and_updates_score():
    engine = make_engine(capacity=3)
    p1, p2 = engine.state.players
    give(p1, engine, STAR_AND_STRIPES, "naruto_05")
    give(p2, engine, "naruto_06")
    star, friendly = p1.hand
    base = engine.cards[STAR_AND_STRIPES].base_points

    # The bonus applies while the row is still mostly empty, so it is not a
    # final-slot check.
    place(engine, star, 0, 0)
    row = engine.state.rows[0]
    assert not row.is_full
    assert star.current_points == base + 3
    engine._reveal_card(row, 0, star)
    scores = calculate_row_scores(row, (p1.id, p2.id), engine.cards)
    assert scores[p1.id] == base + 3

    # A later opposing placement counts too, and removes the bonus as soon as
    # the new card becomes the row's latest placement.
    opposing = p2.hand[0]
    place(engine, opposing, 0, 1)
    assert star.revealed
    assert star.current_points == base
    scores = calculate_row_scores(row, (p1.id, p2.id), engine.cards)
    assert scores[p1.id] == base


def test_star_and_stripes_bonus_removed_by_later_friendly_placement():
    engine = make_engine(capacity=3)
    p1, p2 = engine.state.players
    give(p1, engine, STAR_AND_STRIPES, "naruto_05")
    give(p2, engine, "naruto_06")
    star, friendly = p1.hand
    base = engine.cards[STAR_AND_STRIPES].base_points

    place(engine, star, 0, 0)
    assert star.current_points == base + 3
    place(engine, p2.hand[0], 1, 0)
    place(engine, friendly, 0, 1)
    assert star.revealed
    assert star.current_points == base
    scores = calculate_row_scores(engine.state.rows[0], (p1.id, p2.id), engine.cards)
    assert scores[p1.id] == base


def test_star_and_stripes_bonus_recalculation_is_idempotent_and_can_return():
    engine = make_engine(capacity=3)
    p1, p2 = engine.state.players
    give(p1, engine, STAR_AND_STRIPES, "naruto_05")
    give(p2, engine, "naruto_06")
    star, later_friendly = p1.hand
    base = engine.cards[STAR_AND_STRIPES].base_points

    place(engine, star, 0, 0)
    for _ in range(5):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
        assert star.current_points == base + 3
    place(engine, p2.hand[0], 1, 0)
    place(engine, later_friendly, 0, 1)
    assert star.current_points == base

    # Under the current left-to-right placement rules, a later valid action
    # cannot make an earlier card latest again. Recalculation remains stable.
    for _ in range(5):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
        assert star.current_points == base


def gon_activation_fixture():
    engine = make_engine(capacity=3)
    p1, p2 = engine.state.players
    gon = instance(engine, "hxh_01", p1.id)
    friendly_fighter = instance(engine, "naruto_05", p1.id)
    friendly_fighter_other_row = instance(engine, "hxh_04", p1.id)
    opposing_fighter = instance(engine, "naruto_06", p2.id)
    friendly_non_fighter = instance(engine, "naruto_07", p1.id)
    eliminated_fighter = instance(engine, "naruto_05", p1.id)

    # Preserve a pre-existing point contribution on one recipient.
    friendly_fighter.applied_bonuses = {"legacy_bonus": 2}
    friendly_fighter.current_points = engine.cards["naruto_05"].base_points + 2
    friendly_fighter_other_row.revealed = True
    opposing_fighter.revealed = True
    eliminated_fighter.eliminated = True
    eliminated_fighter.current_points = 0
    engine.state.rows[0].slots[0] = gon
    engine.state.rows[0].slots[1] = friendly_fighter
    engine.state.rows[1].slots[0] = friendly_fighter_other_row
    engine.state.rows[1].slots[1] = opposing_fighter
    engine.state.rows[2].slots[0] = friendly_non_fighter
    engine.state.rows[2].slots[1] = eliminated_fighter

    engine._reveal_card(engine.state.rows[0], 0, gon)
    return engine, gon, friendly_fighter, friendly_fighter_other_row, opposing_fighter, friendly_non_fighter, eliminated_fighter


def test_gon_bonuses_all_friendly_board_fighters_and_sets_him_to_one():
    (
        engine, gon, friendly_fighter, friendly_fighter_other_row,
        opposing_fighter, friendly_non_fighter, eliminated_fighter,
    ) = gon_activation_fixture()
    base = engine.cards["hxh_01"].base_points
    fighter_base = engine.cards["naruto_05"].base_points
    other_fighter_base = engine.cards["hxh_04"].base_points

    assert gon.current_points == 1
    assert gon.base_points == 1
    assert engine.cards["hxh_01"].base_points == base
    assert friendly_fighter.current_points == fighter_base + 6
    assert friendly_fighter.applied_bonuses["legacy_bonus"] == 2
    assert friendly_fighter.current_points == engine.cards["naruto_05"].base_points + sum(friendly_fighter.applied_bonuses.values())
    assert friendly_fighter_other_row.current_points == other_fighter_base + 4
    assert opposing_fighter.current_points == engine.cards["naruto_06"].base_points
    assert friendly_non_fighter.current_points == engine.cards["naruto_07"].base_points
    assert eliminated_fighter.current_points == 0
    assert not any(key.startswith("gon_jajanken_rock_ultimate_transformation:") for key in opposing_fighter.applied_bonuses)
    assert not any(key.startswith("gon_jajanken_rock_ultimate_transformation:") for key in friendly_non_fighter.applied_bonuses)
    assert not any(key.startswith("gon_jajanken_rock_ultimate_transformation:") for key in eliminated_fighter.applied_bonuses)

    activation = next(
        event for event in engine.state.events
        if event.kind == "ability_activated"
        and event.data.get("ability_id") == "gon_jajanken_rock_ultimate_transformation"
    )
    assert activation.data["current_points"] == 1
    assert activation.data["floating_text"] == "JAJANKEN: ROCK"
    assert activation.data["transformed_card_art"] == "assets/art/card front/Abillity_transform/Gon Adult.png"
    assert activation.data["affected_card_ids"] == [
        friendly_fighter.id, friendly_fighter_other_row.id,
    ]
    row_scores = calculate_row_scores(
        engine.state.rows[1], tuple(player.id for player in engine.state.players), engine.cards,
    )
    assert row_scores[engine.state.players[0].id] == other_fighter_base + 4
    assert row_scores[engine.state.players[1].id] == engine.cards["naruto_06"].base_points


def test_gon_does_not_activate_when_no_friendly_fighters_are_on_board():
    engine = make_engine(capacity=3)
    player = engine.state.players[0]
    gon = instance(engine, "hxh_01", player.id)
    row = engine.state.rows[0]
    row.slots[0] = gon

    engine._reveal_card(row, 0, gon)

    assert gon.current_points == engine.cards["hxh_01"].base_points
    assert "gon_jajanken_rock_ultimate_transformation" not in gon.activated_abilities
    assert not any(
        event.kind in {"ability_activated", "ability_resolved"}
        and event.data.get("ability_id") == "gon_jajanken_rock_ultimate_transformation"
        for event in engine.state.events
    )
    failures = [
        event for event in engine.state.events
        if event.kind == "ability_failed"
        and event.data.get("ability_id") == "gon_jajanken_rock_ultimate_transformation"
    ]
    assert len(failures) == 1
    assert failures[0].message == "Gon Freecss's Jajanken: Rock failed"

    # Duplicate reveal processing cannot announce the same failed attempt again.
    engine.abilities.on_card_revealed(
        engine.state, gon, row, 0, emit_failure=engine._handle_ability_failure,
    )
    assert sum(
        event.kind == "ability_failed"
        and event.data.get("ability_id") == "gon_jajanken_rock_ultimate_transformation"
        for event in engine.state.events
    ) == 1
    assert any(
        ability_id == "gon_jajanken_rock_ultimate_transformation" and owner_id == gon.id
        for _, ability_id, owner_id in engine.abilities.pending_abilities
    )

    # A later Fighter reveal is not a new Gon trigger event.
    fighter = instance(engine, "hxh_04", player.id)
    row.slots[1] = fighter
    engine._reveal_card(row, 1, fighter)
    assert gon.current_points == engine.cards["hxh_01"].base_points
    assert not any(
        event.kind == "ability_activated"
        and event.data.get("ability_id") == "gon_jajanken_rock_ultimate_transformation"
        for event in engine.state.events
    )
    assert any(
        ability_id == "gon_jajanken_rock_ultimate_transformation" and owner_id == gon.id
        for _, ability_id, owner_id in engine.abilities.pending_abilities
    )


def test_gon_bonus_snapshot_does_not_grow_or_stack_after_activation():
    (
        engine, gon, friendly_fighter, friendly_fighter_other_row,
        _opposing_fighter, _friendly_non_fighter, _eliminated_fighter,
    ) = gon_activation_fixture()
    bonus_key = f"gon_jajanken_rock_ultimate_transformation:{gon.id}"
    before = (friendly_fighter.current_points, friendly_fighter_other_row.current_points)

    # Duplicate reveal resolution and ordinary point recalculations cannot
    # apply the same activation twice or restore Gon to base points.
    engine.abilities.on_card_revealed(
        engine.state, gon, engine.state.rows[0], 0,
    )
    for _ in range(4):
        engine.abilities.recalculate_continuous_bonuses(engine.state)
    assert (friendly_fighter.current_points, friendly_fighter_other_row.current_points) == before
    assert gon.current_points == 1
    assert friendly_fighter.applied_bonuses[bonus_key] == 4
    engine.abilities.recalculate_after_elimination(
        engine.state, engine.state.rows[2].slots[1],
    )
    assert friendly_fighter_other_row.applied_bonuses[bonus_key] == 4
    assert friendly_fighter_other_row.current_points == before[1]
    assert gon.current_points == 1

    # A Fighter entering the board after the activation is outside its snapshot.
    p1 = engine.state.players[0]
    give(p1, engine, "naruto_06")
    late_fighter = p1.hand[0]
    place(engine, late_fighter, 3, 0)
    assert late_fighter.current_points == engine.cards["naruto_06"].base_points
    assert bonus_key not in late_fighter.applied_bonuses
    assert gon.current_points == 1

    # Multiplayer views are projections of the same authoritative point state.
    view_one = engine.view_for(engine.state.players[0].id)
    view_two = engine.view_for(engine.state.players[1].id)
    for view in (view_one, view_two):
        assert view["rows"][0]["slots"][0]["card"]["current_points"] == 1
        assert view["rows"][1]["slots"][0]["card"]["current_points"] == before[1]


def test_eliminating_gon_removes_his_bonus_but_preserves_other_contributions():
    (
        engine, gon, fighter, _fighter_other_row,
        _opposing_fighter, _friendly_non_fighter, _eliminated_fighter,
    ) = gon_activation_fixture()
    gon_bonus_key = f"gon_jajanken_rock_ultimate_transformation:{gon.id}"
    sakura_bonus_key = "sakura_naruto_or_sasuke_row_bonus"
    fighter.revealed = True
    naruto = instance(engine, NARUTO, gon.owner_id)
    naruto.revealed = True
    engine.state.rows[0].slots[2] = naruto
    fighter.applied_bonuses[sakura_bonus_key] = 2
    fighter.current_points += 2

    engine.elimination.eliminate_target(engine.state, gon, gon)
    changed = engine.abilities.recalculate_after_elimination(engine.state, gon)

    assert fighter in changed
    assert fighter.eliminated is False
    assert gon.eliminated is True and gon.current_points == 0
    assert gon_bonus_key not in fighter.applied_bonuses
    assert fighter.applied_bonuses[sakura_bonus_key] == 2
    assert fighter.current_points == engine.cards[fighter.definition_id].base_points + 2
    row_scores = calculate_row_scores(
        engine.state.rows[0], tuple(player.id for player in engine.state.players), engine.cards,
    )
    assert row_scores[gon.owner_id] == fighter.current_points + naruto.current_points

    for player in engine.state.players:
        view = engine.view_for(player.id)
        fighter_view = view["rows"][0]["slots"][1]["card"]
        assert fighter_view["current_points"] == fighter.current_points
        assert fighter_view["applied_bonuses"] == fighter.applied_bonuses


def test_eliminating_another_card_does_not_remove_gons_snapshot_bonus():
    (
        engine, gon, fighter, fighter_other_row,
        _opposing_fighter, _friendly_non_fighter, eliminated_fighter,
    ) = gon_activation_fixture()
    gon_bonus_key = f"gon_jajanken_rock_ultimate_transformation:{gon.id}"

    assert gon_bonus_key in fighter.applied_bonuses
    engine.abilities.recalculate_after_elimination(engine.state, eliminated_fighter)

    assert gon.eliminated is False
    assert fighter.applied_bonuses[gon_bonus_key] == 4
    assert fighter_other_row.applied_bonuses[gon_bonus_key] == 4


def test_naruto_elimination_ability_definition():
    engine = make_engine()
    # The first ability on Naruto should be the elimination ability
    elimination_ability = engine.cards[NARUTO].abilities[0]
    assert elimination_ability.id == "naruto_rasenshuriken_eliminate"
    assert elimination_ability.trigger == "on_reveal"
    assert [e.type for e in elimination_ability.effects] == ["eliminate_target"]

