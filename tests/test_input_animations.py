from types import SimpleNamespace

import pygame

from conftest import resolve_eliminations
from veilbound.client.input import InputController
from veilbound.client.renderer import Renderer
from veilbound.shared.config import GameConfig
from veilbound.shared.engine import GameEngine
from veilbound.shared.models import GameEvent, GamePhase


class RecordingRenderer:
    animation_ms = {
        "card_flip_ms": 100,
        "delay_between_reveals_ms": 100,
        "card_information_ms": 100,
        "score_count_ms": 100,
        "row_result_ms": 100,
        "row_transition_ms": 100,
        "final_round_result_ms": 100,
        "duo_energy_ms": 100,
    }

    def __init__(self):
        self.events = []

    def note_events(self, events):
        self.events.extend(events)

    def reset_animations(self):
        pass

    @property
    def duo_animation_pending(self) -> bool:
        return False

    @property
    def duo_animation_deadline(self) -> float:
        return 0.0


def test_animation_flow_advances_by_deadlines_without_blocking():
    engine = GameEngine(
        GameConfig(
            deck_size=40,
            initial_hand_size=5,
            draw_per_turn=1,
            capacity_min=1,
            capacity_max=1,
            rounds_per_match=2,
        ),
        seed=3,
    )
    while engine.state.phase == GamePhase.PLACEMENT:
        player = engine.state.active_player
        row_id, slot_index = engine.legal_slots()[0]
        assert engine.play_card(player.id, player.hand[0].id, row_id, slot_index).accepted

    renderer = RecordingRenderer()
    controller = InputController(engine, renderer)

    now = 0.0
    controller.update(now)
    assert controller.animation_stage == "card_flip"
    assert engine.state.phase == GamePhase.REVEAL
    assert engine.state.rows[0].slots[0].revealed
    controller.update(0.099)
    assert controller.animation_stage == "card_flip"
    controller.update(0.1)
    assert controller.animation_stage == "card_info"

    now = 0.2
    for _ in range(250):
        if engine.state.phase == GamePhase.PLACEMENT:
            while engine.state.phase == GamePhase.PLACEMENT:
                resolve_eliminations(engine)
                if engine.state.phase != GamePhase.PLACEMENT:
                    break
                player = engine.state.active_player
                row_id, slot_index = engine.legal_slots()[0]
                assert engine.play_card(player.id, player.hand[0].id, row_id, slot_index).accepted
        controller.update(now)
        # Godspeed arms a target-selection pause mid-reveal; settle it so the
        # deadline-driven flow can continue without an interactive player.
        resolve_eliminations(engine)
        now += 0.1
        if engine.state.phase == GamePhase.MATCH_RESULTS:
            break

    assert engine.state.phase == GamePhase.MATCH_RESULTS
    kinds = [event.kind for event in renderer.events]
    assert kinds.count("card_revealed") == 8
    assert kinds.count("row_scored") == 8
    assert kinds.count("next_row") == 6
    first_round_reveals = [
        event.data["row_number"] for event in renderer.events
        if event.kind == "card_revealed"
    ][:4]
    assert first_round_reveals == [1, 2, 3, 4]
    first_scored = next(i for i, event in enumerate(renderer.events) if event.kind == "row_scored")
    first_round_last_reveal = [
        i for i, event in enumerate(renderer.events) if event.kind == "card_revealed"
    ][:4][-1]
    assert first_scored > first_round_last_reveal


def test_final_reveals_wait_for_each_flip_and_finish_before_scoring():
    engine = GameEngine(
        GameConfig(
            deck_size=40,
            initial_hand_size=5,
            draw_per_turn=1,
            capacity_min=1,
            capacity_max=1,
            rounds_per_match=2,
        ),
        seed=5,
    )
    while engine.state.phase == GamePhase.PLACEMENT:
        player = engine.state.active_player
        row_id, slot_index = engine.legal_slots()[0]
        assert engine.play_card(player.id, player.hand[0].id, row_id, slot_index).accepted

    renderer = RecordingRenderer()
    controller = InputController(engine, renderer)
    controller.update(0.0)
    assert [event.data["row_number"] for event in renderer.events if event.kind == "card_revealed"] == [1]

    controller.update(0.099)
    assert len([event for event in renderer.events if event.kind == "card_revealed"]) == 1
    controller.update(0.1)
    controller.update(0.2)
    assert len([event for event in renderer.events if event.kind == "card_revealed"]) == 1
    controller.update(0.301)
    assert [event.data["row_number"] for event in renderer.events if event.kind == "card_revealed"] == [1, 2]

    now = 0.302
    while len([event for event in renderer.events if event.kind == "card_revealed"]) < 4:
        controller.update(now)
        now += 0.05
    reveal_events = [event for event in renderer.events if event.kind == "card_revealed"]
    assert [event.data["row_number"] for event in reveal_events] == [1, 2, 3, 4]
    assert len({event.data["card_id"] for event in reveal_events}) == 4
    assert not any(event.kind == "row_scored" for event in renderer.events)
    assert engine.state.phase == GamePhase.ROW_SCORING
    assert controller.animation_stage == "card_flip"

    controller.update(now + 0.099)
    assert not any(event.kind == "row_scored" for event in renderer.events)
    controller.update(now + 0.1)
    assert not any(event.kind == "row_scored" for event in renderer.events)
    controller.update(now + 0.2)
    assert not any(event.kind == "row_scored" for event in renderer.events)
    controller.update(now + 0.21)
    assert [event.kind for event in renderer.events].count("row_scored") == 1


def test_placement_reveal_uses_card_flip_and_information_stages():
    engine = GameEngine(
        GameConfig(
            deck_size=40,
            initial_hand_size=5,
            draw_per_turn=1,
            capacity_min=3,
            capacity_max=3,
            rounds_per_match=2,
        ),
        seed=9,
    )
    row = engine.state.rows[0]
    first_player = engine.state.active_player
    assert engine.play_card(first_player.id, first_player.hand[0].id, row.id, 0).accepted
    renderer = RecordingRenderer()
    controller = InputController(engine, renderer)

    second_player = engine.state.active_player
    placement = engine.play_card(second_player.id, second_player.hand[0].id, row.id, 1)
    controller._apply(placement)

    assert controller.animation_stage == "card_flip"
    assert [event.data["slot_index"] for event in renderer.events if event.kind == "card_revealed"] == [0]
    flip_deadline = controller.animation_deadline
    controller.update(flip_deadline)
    assert controller.animation_stage == "card_info"
    info_deadline = controller.animation_deadline
    controller.update(info_deadline)
    assert controller.animation_stage is None
    assert engine.state.phase == GamePhase.PLACEMENT


def test_renderer_ignores_duplicate_reveal_event_without_restarting_flip():
    renderer = Renderer.__new__(Renderer)
    renderer.flip_started = {}
    renderer.last_card_reveal = ""
    renderer.reveal_card = None
    renderer.reveal_card_visible_after = 0.0
    renderer.reveal_card_until = 0.0
    renderer.score_animation_row_id = None
    renderer.score_animation_started = 0.0
    renderer.animation_ms = RecordingRenderer.animation_ms
    renderer.notice = ""
    renderer.notice_until = 0.0
    reveal = GameEvent("card_revealed", "revealed", {
        "row_id": "row-1", "slot_index": 0, "name": "Test character",
    })

    renderer.note_events((reveal,))
    first_start = renderer.flip_started[("row-1", 0)]
    renderer.note_events((reveal,))

    assert renderer.flip_started == {("row-1", 0): first_start}


def test_renderer_preserves_back_shrink_face_expand_flip_transition():
    renderer = Renderer.__new__(Renderer)
    renderer.flip_started = {("row-1", 0): 10.0}
    renderer.animation_ms = {"card_flip_ms": 100}
    renderer._make_card_back_surface = lambda size, owner_id: _solid_surface(size, (0, 0, 255))
    renderer._make_card_face = lambda definition, size: _solid_surface(size, (255, 0, 0))
    renderer._draw_card_frame = lambda screen, rect: None
    row = SimpleNamespace(id="row-1")
    card = SimpleNamespace(definition_id="definition-1", owner_id="player-1", revealed=True)
    definition = SimpleNamespace(id="definition-1")
    rect = pygame.Rect(20, 20, 40, 60)

    def sample(elapsed, point):
        screen = pygame.Surface((100, 100))
        screen.fill((1, 1, 1))
        renderer._draw_placed_card(screen, rect, row, 0, card, {"definition-1": definition}, 10.0 + elapsed)
        return screen.get_at(point)[:3]

    assert sample(0.0, (23, 50)) == (0, 0, 255)
    assert sample(0.025, (23, 50)) == (1, 1, 1)  # card narrows around the center
    assert sample(0.025, (40, 50)) == (0, 0, 255)
    midpoint = sample(0.05, (40, 50))
    assert midpoint[0] > 240 and midpoint[1:] == (0, 0)  # face changes at the midpoint
    assert sample(0.075, (23, 50)) == (1, 1, 1)  # front widens after the midpoint
    finished = sample(0.1, (23, 50))
    assert finished[0] > 240 and finished[1:] == (0, 0)


def _solid_surface(size, color):
    surface = pygame.Surface(size)
    surface.fill(color)
    return surface
