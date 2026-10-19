from copy import deepcopy
import json
import logging

import pygame

from conftest import resolve_eliminations
from veilbound.client.layout import BoardLayout
from veilbound.client.renderer import Renderer
from veilbound.shared.abilities import AbilityDefinition
from veilbound.shared.config import GameConfig
from veilbound.shared.definitions import DATA_DIR, deal_player_pool, load_card_definitions
from veilbound.shared.engine import GameEngine
from veilbound.shared.models import GamePhase
from veilbound.shared.scoring import calculate_row_scores


def fixed_engine(capacity=2, rounds=2, seed=4):
    return GameEngine(
        GameConfig(
            deck_size=40,
            initial_hand_size=5,
            draw_per_turn=1,
            capacity_min=capacity,
            capacity_max=capacity,
            rounds_per_match=rounds,
        ),
        seed=seed,
    )


def fill_board(engine):
    while engine.state.phase == GamePhase.PLACEMENT:
        # Killua-style abilities can arm an elimination mid-placement when a
        # reveal resolves; settle those before the next legal-slot lookup.
        resolve_eliminations(engine)
        player = engine.state.active_player
        row_id, slot_index = engine.legal_slots()[0]
        result = engine.play_card(player.id, player.hand[0].id, row_id, slot_index)
        assert result.accepted
    resolve_eliminations(engine)


def finish_row(engine):
    while engine.state.phase == GamePhase.REVEAL:
        resolve_eliminations(engine)
        if engine.state.phase != GamePhase.REVEAL:
            # Resolving the final reveal's elimination completed the pass.
            break
        result = engine.reveal_next_card()
        assert result.accepted
    resolve_eliminations(engine)
    assert engine.state.phase == GamePhase.ROW_SCORING
    return engine.score_current_row()


def finish_round(engine):
    while engine.state.phase not in {GamePhase.ROUND_RESULTS, GamePhase.MATCH_RESULTS}:
        resolve_eliminations(engine)
        if engine.state.phase == GamePhase.REVEAL:
            result = engine.reveal_next_card()
            assert result.accepted
        elif engine.state.phase == GamePhase.ROW_SCORING:
            result = engine.score_current_row()
            assert result.accepted
        elif engine.state.phase == GamePhase.ROW_RESULTS:
            result = engine.advance_after_row_result()
            assert result.accepted
        else:
            raise AssertionError(f"Unexpected phase: {engine.state.phase}")
    resolve_eliminations(engine)


def player_pool_ids(player):
    return {
        card.definition_id
        for card in player.draw_pile + player.hand + player.used
    }


def test_game_starts_with_four_rows_random_capacities_and_matching_slots():
    engine = GameEngine(seed=13)
    assert len(engine.state.rows) == 4
    assert [row.number for row in engine.state.rows] == [1, 2, 3, 4]
    for row in engine.state.rows:
        assert 1 <= row.capacity <= 10
        assert len(row.slots) == row.capacity
        assert row.placed_count == 0
        assert row.slots == [None] * row.capacity


def test_roster_and_independent_match_pools_have_expected_size_and_unique_cards():
    engine = GameEngine(seed=13)
    roster_ids = set(engine.cards)
    assert len(roster_ids) == 50

    for player in engine.state.players:
        pool = player.draw_pile + player.hand + player.used
        ids = [card.definition_id for card in pool]
        assert len(ids) == 40
        assert len(set(ids)) == 40
        assert set(ids) <= roster_ids


def test_seeded_player_pool_is_reproducible_and_every_roster_card_can_be_dealt():
    roster_ids = tuple(load_card_definitions())
    first = deal_player_pool(roster_ids, seed=321)
    assert first == deal_player_pool(roster_ids, seed=321)
    assert len(first) == len(set(first)) == 40
    assert set(first) <= set(roster_ids)

    seen = set()
    for seed in range(200):
        seen.update(deal_player_pool(roster_ids, seed=seed))
    assert seen == set(roster_ids)


def test_roster_loader_warns_for_missing_duplicate_and_extra_ids(tmp_path, caplog):
    source_path = DATA_DIR / "anime_bedriegers_bedrogen.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))

    missing = deepcopy(source)
    missing["cards"].pop()
    missing_path = tmp_path / "missing.json"
    missing_path.write_text(json.dumps(missing), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        load_card_definitions(missing_path)
    assert "missing 1 unique character" in caplog.text

    caplog.clear()
    duplicate = deepcopy(source)
    duplicate["cards"].append(deepcopy(duplicate["cards"][0]))
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        load_card_definitions(duplicate_path)
    assert "duplicate IDs" in caplog.text

    caplog.clear()
    extra = deepcopy(source)
    extra_entry = deepcopy(extra["cards"][0])
    extra_entry["id"] = "extra_test_character"
    extra["cards"].append(extra_entry)
    extra_path = tmp_path / "extra.json"
    extra_path.write_text(json.dumps(extra), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        load_card_definitions(extra_path)
    assert "1 extra unique character" in caplog.text


def test_board_layout_has_one_visible_target_for_every_required_slot():
    engine = GameEngine(seed=19)
    layout = BoardLayout((1280, 900), engine.state)
    expected_slots = sum(row.capacity for row in engine.state.rows)
    assert len(layout.slot_rects) == expected_slots
    assert set(layout.slot_rects) == {
        (row.id, index)
        for row in engine.state.rows
        for index in range(row.capacity)
    }


def test_players_alternate_placements_and_face_down_cards_are_hidden():
    engine = fixed_engine(capacity=2)
    p1, p2 = engine.state.players
    row = engine.state.rows[0]
    first = engine.play_card(p1.id, p1.hand[0].id, row.id, 0)
    assert first.accepted
    assert engine.state.active_player.id == p2.id
    assert row.slots[0].owner_id == p1.id
    assert not row.slots[0].revealed
    view = engine.view_for(p2.id)
    hidden_slot = view["rows"][0]["slots"][0]
    assert hidden_slot["occupied"] is True
    assert hidden_slot["owner_id"] == p1.id
    assert hidden_slot["card"] is None
    assert next(player for player in view["players"] if player["id"] == p1.id)["hand"] is None


def test_full_slots_reject_placement_without_changing_state():
    engine = fixed_engine(capacity=1)
    row = engine.state.rows[0]
    p1 = engine.state.active_player
    assert engine.play_card(p1.id, p1.hand[0].id, row.id, 0).accepted
    active = engine.state.active_player
    before = deepcopy(engine.state)
    result = engine.play_card(active.id, active.hand[0].id, row.id, 0)
    assert not result.accepted
    assert "occupied" in result.reason
    assert engine.state == before
    assert len(row.slots) == row.capacity == 1


def test_each_row_only_accepts_the_next_slot_and_rejections_preserve_hand():
    engine = fixed_engine(capacity=3)
    row = engine.state.rows[0]
    p1, p2 = engine.state.players

    assert (row.id, 0) in engine.legal_slots()
    before = deepcopy(engine.state)
    skipped = engine.play_card(p1.id, p1.hand[0].id, row.id, 1)
    assert not skipped.accepted
    assert "next empty slot" in skipped.reason
    assert engine.state == before

    first = engine.play_card(p1.id, p1.hand[0].id, row.id, 0)
    assert first.accepted
    assert (row.id, 1) in engine.legal_slots()
    before = deepcopy(engine.state)
    skipped = engine.play_card(p2.id, p2.hand[0].id, row.id, 2)
    assert not skipped.accepted
    assert engine.state == before

    second = engine.play_card(p2.id, p2.hand[0].id, row.id, 1)
    assert second.accepted
    assert (row.id, 2) in engine.legal_slots()
    before = deepcopy(engine.state)
    overwrite = engine.play_card(p1.id, p1.hand[0].id, row.id, 1)
    assert not overwrite.accepted
    assert "occupied" in overwrite.reason
    assert engine.state == before
    assert engine.play_card(p1.id, p1.hand[0].id, row.id, 2).accepted


def test_placement_without_an_occupied_neighbor_does_not_reveal_a_card():
    engine = fixed_engine(capacity=3)
    row = engine.state.rows[0]
    player = engine.state.active_player

    placed = engine.play_card(player.id, player.hand[0].id, row.id, 0)

    assert placed.accepted
    assert not row.slots[0].revealed
    assert not any(event.kind == "card_revealed" for event in placed.events)
    assert engine.state.active_player.id != player.id


def test_adjacent_placement_reveals_only_the_previous_card():
    engine = fixed_engine(capacity=3)
    row = engine.state.rows[0]
    p1, p2 = engine.state.players
    first = engine.play_card(p1.id, p1.hand[0].id, row.id, 0)
    assert first.accepted
    assert not row.slots[0].revealed

    second = engine.play_card(p2.id, p2.hand[0].id, row.id, 1)

    assert second.accepted
    reveals = [event for event in second.events if event.kind == "card_revealed"]
    assert len(reveals) == 1
    assert reveals[0].data["row_id"] == row.id
    assert reveals[0].data["slot_index"] == 0
    assert reveals[0].data["card_id"] == row.slots[0].id
    assert row.slots[0].revealed and not row.slots[1].revealed
    assert calculate_row_scores(row, (p1.id, p2.id), engine.cards) == {
        p1.id: engine.cards[row.slots[0].definition_id].base_points,
        p2.id: 0,
    }
    assert engine.state.rows[1].slots[0] is None
    assert engine.state.active_player.id == p1.id
    assert not engine.reveal_next_card().accepted

    third = engine.play_card(p1.id, p1.hand[0].id, row.id, 2)
    assert third.accepted
    next_reveals = [event for event in third.events if event.kind == "card_revealed"]
    assert len(next_reveals) == 1
    assert next_reveals[0].data["slot_index"] == 1
    assert row.slots[0].revealed and row.slots[1].revealed and not row.slots[2].revealed


def test_row_box_totals_include_only_revealed_cards():
    engine = fixed_engine(capacity=2)
    row = engine.state.rows[0]
    p1, p2 = engine.state.players
    assert engine.play_card(p1.id, p1.hand[0].id, row.id, 0).accepted
    assert engine.play_card(p2.id, p2.hand[0].id, row.id, 1).accepted

    renderer = Renderer.__new__(Renderer)
    labels = []
    renderer._text_center_fit = lambda _screen, text, *_args, **_kwargs: labels.append(text)
    renderer._text_center = lambda _screen, text, *_args, **_kwargs: labels.append(text)
    renderer._draw_row_box(
        pygame.Surface((120, 120)), row, pygame.Rect(0, 0, 80, 90), engine.state, engine.cards,
    )

    visible_points = engine.cards[row.slots[0].definition_id].base_points
    assert f"P1 {visible_points}" in labels
    assert "P2 0" in labels


def test_final_reveal_scans_only_remaining_cards_top_to_bottom_before_scoring():
    engine = fixed_engine(capacity=1)
    rows = engine.state.rows
    # Row 2 has two cards, so its first card reveals when the second is placed.
    rows[1].capacity = 2
    rows[1].slots = [None, None]
    for slot_index in (0, 1):
        player = engine.state.active_player
        placed = engine.play_card(player.id, player.hand[0].id, rows[1].id, slot_index)
        assert placed.accepted
        placement_reveals = [event for event in placed.events if event.kind == "card_revealed"]
        if slot_index == 0:
            assert placement_reveals == []
        else:
            assert len(placement_reveals) == 1
            assert placement_reveals[0].data["row_number"] == 2
            assert placement_reveals[0].data["slot_index"] == 0
    assert rows[1].slots[0].revealed and not rows[1].slots[1].revealed

    fill_board(engine)
    assert engine.state.phase == GamePhase.REVEAL
    assert not engine.score_current_row().accepted

    final_reveals = []
    while engine.state.phase == GamePhase.REVEAL:
        result = engine.reveal_next_card()
        assert result.accepted
        final_reveals.extend(event.data["row_number"] for event in result.events if event.kind == "card_revealed")
        if engine.state.phase == GamePhase.REVEAL:
            assert not engine.score_current_row().accepted

    assert final_reveals == [1, 2, 3, 4]
    assert engine.state.phase == GamePhase.ROW_SCORING
    assert all(card is not None and card.revealed for row in rows for card in row.slots)
    assert engine.score_current_row().accepted
    assert engine.advance_after_row_result().accepted
    assert engine.state.phase == GamePhase.ROW_SCORING
    assert engine.score_current_row().accepted


def test_round_enters_reveal_only_after_all_required_cards_are_placed():
    engine = fixed_engine(capacity=1)
    assert not engine.score_current_row().accepted
    fill_board(engine)
    assert engine.state.phase == GamePhase.REVEAL
    assert all(row.is_full for row in engine.state.rows)
    assert engine.legal_slots() == []
    active = engine.state.active_player
    before = deepcopy(engine.state)
    result = engine.play_card(active.id, active.hand[0].id, engine.state.rows[0].id, 0)
    assert not result.accepted
    assert engine.state == before


def test_reveals_and_scores_in_top_to_bottom_order():
    engine = fixed_engine(capacity=1)
    fill_board(engine)
    rows = engine.state.rows
    assert engine.state.row_index == 0
    assert all(row.slots[0] is not None and not row.slots[0].revealed for row in rows)

    reveal = engine.reveal_next_card()
    assert reveal.accepted
    assert reveal.events[0].data["effect_name"]
    assert reveal.events[0].data["effect_text"]
    assert rows[0].slots[0].revealed
    assert all(not row.slots[0].revealed for row in rows[1:])
    assert engine.state.phase == GamePhase.REVEAL
    for row in rows[1:]:
        next_reveal = engine.reveal_next_card()
        assert next_reveal.accepted
        assert next_reveal.events[0].data["row_id"] == row.id
    assert engine.state.phase == GamePhase.ROW_SCORING
    score = engine.score_current_row()
    assert score.accepted
    assert not any(event.kind == "card_revealed" for event in score.events)
    assert engine.state.phase == GamePhase.ROW_RESULTS
    before_points = [player.match_points for player in engine.state.players]
    assert not engine.score_current_row().accepted
    assert [player.match_points for player in engine.state.players] == before_points

    next_row = engine.advance_after_row_result()
    assert next_row.accepted
    assert not any(event.kind == "card_revealed" for event in next_row.events)
    assert engine.state.phase == GamePhase.ROW_SCORING
    assert engine.state.row_index == 1
    assert rows[1].slots[0].revealed
    finish_round(engine)
    assert engine.state.phase == GamePhase.ROUND_RESULTS
    assert engine.state.scored_row_order == [row.id for row in rows]


def test_scores_are_owner_sums_and_capacity_points_are_awarded_once():
    engine = fixed_engine(capacity=2)
    p1, p2 = engine.state.players
    p1.hand = [engine._new_card(card_id, p1.id) for card_id in ("naruto_01", "naruto_10", "naruto_09", "naruto_09")]
    p2.hand = [engine._new_card(card_id, p2.id) for card_id in ("naruto_04", "naruto_01", "naruto_10", "naruto_09")]
    planned = [
        (0, 0), (0, 1),
        (1, 0), (1, 1),
        (2, 0), (2, 1),
        (3, 0), (3, 1),
    ]
    for row_index, slot_index in planned:
        player = engine.state.active_player
        result = engine.play_card(player.id, player.hand[0].id, engine.state.rows[row_index].id, slot_index)
        assert result.accepted

    finish_row(engine)
    first_row = engine.state.rows[0]
    # p1's Naruto (10) beats p2's Sakura (base 6 after the roster rebalance).
    assert first_row.scores == {p1.id: 10, p2.id: 6}
    assert first_row.winner_id == p1.id
    assert first_row.awarded_points == first_row.capacity == 2
    assert p1.match_points == 2
    assert not engine.score_current_row().accepted
    assert p1.match_points == 2

    engine.advance_after_row_result()
    finish_row(engine)
    second_row = engine.state.rows[1]
    assert second_row.scores == {p1.id: 4, p2.id: 10}
    assert second_row.winner_id == p2.id
    assert p2.match_points == 2

    engine.advance_after_row_result()
    finish_round(engine)
    assert engine.state.rows[2].scores == {p1.id: 5, p2.id: 4}
    assert engine.state.rows[2].winner_id == p1.id
    assert engine.state.rows[2].awarded_points == 2
    assert engine.state.rows[3].scores == {p1.id: 5, p2.id: 5}
    assert engine.state.rows[3].winner_id is None
    assert engine.state.rows[3].awarded_points == 0
    assert p1.match_points == 4
    assert p2.match_points == 2


def test_tied_rows_have_no_winner_and_award_no_points():
    engine = fixed_engine(capacity=2)
    for player in engine.state.players:
        player.hand = [engine._new_card("naruto_09", player.id) for _ in range(4)]
    fill_board(engine)
    finish_round(engine)

    for row in engine.state.rows:
        expected_score = engine.cards["naruto_09"].base_points
        assert row.scores == {"player-1": expected_score, "player-2": expected_score}
        assert row.winner_id is None
        assert row.awarded_points == 0
    assert engine.state.player("player-1").match_points == 0
    assert engine.state.player("player-2").match_points == 0
    assert engine.state.round_winner_id is None


def test_cumulative_points_survive_round_reset_and_final_match_result():
    engine = fixed_engine(capacity=1, seed=17)
    fill_board(engine)
    finish_round(engine)
    points_after_round_one = [player.match_points for player in engine.state.players]
    first_row_ids = [row.id for row in engine.state.rows]

    result = engine.continue_after_round_results()
    assert result.accepted
    assert engine.state.round_number == 2
    assert engine.state.active_player.id == "player-2"
    assert [player.match_points for player in engine.state.players] == points_after_round_one
    assert [row.id for row in engine.state.rows] != first_row_ids
    assert all(not row.scored and row.slots == [None] for row in engine.state.rows)
    assert engine.state.scored_row_order == []

    fill_board(engine)
    finish_round(engine)
    assert engine.state.phase == GamePhase.ROUND_RESULTS
    assert engine.continue_after_round_results().accepted
    assert engine.state.phase == GamePhase.MATCH_RESULTS
    assert engine.state.winner_id in {None, "player-1", "player-2"}


def test_pools_hands_and_used_cards_persist_across_rounds_without_reuse():
    engine = fixed_engine(capacity=1, seed=17)
    p1, p2 = engine.state.players
    played_in_round_one = p1.hand[0]
    held_across_rounds = p1.hand[-1]

    assert engine.play_card(p1.id, played_in_round_one.id, engine.state.rows[0].id, 0).accepted
    fill_board(engine)
    finish_round(engine)
    assert played_in_round_one in p1.used
    assert held_across_rounds in p1.hand
    p1_draw_pile_at_round_end = [card.id for card in p1.draw_pile]
    p1_hand_at_round_end = [card.id for card in p1.hand]
    p1_used_at_round_end = [card.id for card in p1.used]
    pool_at_round_end = player_pool_ids(p1)
    p2_pool_at_round_end = player_pool_ids(p2)

    assert engine.continue_after_round_results().accepted
    assert engine.state.round_number == 2
    assert player_pool_ids(p1) == pool_at_round_end
    assert player_pool_ids(p2) == p2_pool_at_round_end
    assert [card.id for card in p1.draw_pile] == p1_draw_pile_at_round_end
    assert [card.id for card in p1.hand] == p1_hand_at_round_end
    assert [card.id for card in p1.used] == p1_used_at_round_end
    assert held_across_rounds in p1.hand

    # Round two starts with Player 2, then Player 1 gets a turn. The round-one
    # card is still unavailable even though its old row is no longer on screen.
    assert engine.state.active_player is p2
    assert engine.play_card(p2.id, p2.hand[0].id, engine.state.rows[0].id, 0).accepted
    assert engine.state.active_player is p1
    row_id, slot_index = engine.legal_slots()[0]
    rejected = engine.play_card(p1.id, played_in_round_one.id, row_id, slot_index)
    assert not rejected.accepted
    assert "not in the player's hand" in rejected.reason
    assert played_in_round_one in p1.used

    fill_board(engine)
    finish_round(engine)
    round_two_p1_cards = {
        card.id
        for row in engine.state.rows
        for card in row.slots
        if card is not None and card.owner_id == p1.id
    }
    assert played_in_round_one.id not in round_two_p1_cards
    assert set(p1_used_at_round_end).isdisjoint(round_two_p1_cards)
    assert len({card.id for card in p1.used}) == len(p1.used)


def test_empty_draw_pile_never_refills_from_used_cards_and_warns_once(caplog):
    engine = fixed_engine(capacity=1)
    player = engine.state.active_player
    played = player.hand.pop()
    player.used.append(played)
    player.draw_pile.clear()
    player.hand.clear()

    with caplog.at_level(logging.WARNING):
        engine._draw_for_active_player()
        engine._draw_for_active_player()
    assert player.hand == []
    assert player.used == [played]
    assert player.draw_pile == []
    assert caplog.text.count("draw pile ran out while drawing") == 1


def test_restart_resets_match_points_and_assigns_new_card_instance_ids():
    engine = fixed_engine(capacity=1)
    old_ids = {
        card.id
        for player in engine.state.players
        for card in player.hand + player.draw_pile + player.used
    }
    engine.state.players[0].match_points = 12
    engine.start_new_match(seed=22)
    new_ids = {
        card.id
        for player in engine.state.players
        for card in player.hand + player.draw_pile + player.used
    }
    assert old_ids.isdisjoint(new_ids)
    assert [player.match_points for player in engine.state.players] == [0, 0]
    assert engine.state.round_number == 1
    assert engine.state.phase == GamePhase.PLACEMENT


def test_character_definitions_use_new_source_fields_and_link_images():
    engine = GameEngine(seed=5)
    for definition in engine.cards.values():
        assert definition.name
        assert definition.image.startswith("assets/art/characters/")
        assert isinstance(definition.base_points, int)
        assert definition.effect_name
        assert definition.effect_text
        # Abilities are optional character data now: only characters that
        # declare one (currently Sasuke) carry a parsed AbilityDefinition.
        assert definition.ability is None or isinstance(definition.ability, AbilityDefinition)

