"""Multiplayer coverage: protocol framing, two-peer room flow, and remote projection."""

from __future__ import annotations

import asyncio
import queue

import pytest

from veilbound.client.menu import MenuScreen, normalize_room_code
from veilbound.client.network import RemoteGameEngine
from veilbound.shared.config import GameConfig
from veilbound.shared.engine import GameEngine
from veilbound.shared.models import GamePhase
from veilbound.shared.protocol import MAX_MESSAGE_BYTES, decode_message, encode_message
from veilbound.server.rooms import Room


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


def test_protocol_round_trip_and_rejections():
    message = {"type": "update", "locked": False, "view": {"a": 1}, "events": []}
    assert decode_message(encode_message(message).rstrip(b"\n")) == message
    with pytest.raises(ValueError):
        decode_message(b"x" * (MAX_MESSAGE_BYTES + 1))
    with pytest.raises(ValueError):
        decode_message(b"[1, 2]")
    with pytest.raises(ValueError):
        decode_message(b'{"no_type": true}')


def test_room_code_normalization():
    assert normalize_room_code("ab-12 cd34") == "AB12CD"
    assert normalize_room_code("  xy9z  ") == "XY9Z"
    assert normalize_room_code("TOOLONGCODE123") == "TOOLON"


def test_menu_root_buttons_route_to_expected_outcomes():
    menu = MenuScreen()
    size = (1280, 900)
    assert menu.button_at(menu.layout(size)["local"].center, size) == "local"
    assert menu.button_at(menu.layout(size)["host"].center, size) == "host"
    assert menu.button_at(menu.layout(size)["join"].center, size) == "join"
    assert menu.button_at(menu.layout(size)["quit"].center, size) == "quit"
    assert menu.button_at((10, 10), size) is None

    #__PART2__


class FakeWriter:
    """Minimal asyncio writer stand-in that captures what the room sends."""

    def __init__(self):
        self.messages: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.messages.append(decode_message(bytes(data).rstrip(b"\n")))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def updates(self) -> list[dict]:
        return [message for message in self.messages if message.get("type") == "update"]

    def errors(self) -> list[dict]:
        return [message for message in self.messages if message.get("type") == "error"]


class StubConnection:
    """Thread-free stand-in for NetworkConnection for engine projection tests."""

    def __init__(self, player_id: str | None = None, room_code: str | None = None):
        self.incoming: queue.Queue = queue.Queue()
        self.sent: list[dict] = []
        self.player_id = player_id
        self.room_code = room_code

    def send(self, message: dict) -> None:
        self.sent.append(message)


def push_update(connection: StubConnection, engine: GameEngine, viewer_id: str, locked: bool, events=()) -> None:
    view = engine.view_for(viewer_id)
    connection.incoming.put({
        "type": "update", "view": view, "events": list(events), "locked": locked,
    })


def test_remote_engine_rebuilds_masked_state_and_gates_input():
    engine = fixed_engine()
    connection = StubConnection(player_id="player-1", room_code="ABC123")
    remote = RemoteGameEngine(connection)

    assert remote.state is None
    assert remote.legal_slots() == []

    push_update(connection, engine, "player-1", locked=True)
    remote.poll()
    assert remote.state is not None
    assert remote.state.viewer_id == "player-1"
    assert remote.legal_slots() == []  # locked room: no input allowed

    push_update(connection, engine, "player-1", locked=False)
    remote.poll()
    legal = remote.legal_slots()
    assert legal  # unlocked room: server-advertised slots are accepted
    assert all(isinstance(row_id, str) and isinstance(index, int) for row_id, index in legal)

    own_hand = remote.state.player("player-1").hand
    opponent_hand = remote.state.player("player-2").hand
    # The opening player receives a start-of-turn draw on top of the 5-card hand.
    assert len(own_hand) == 6
    assert [card.definition_id for card in own_hand]  # own definitions are visible
    assert opponent_hand == []  # opponent hand is masked

    viewer = engine.view_for("player-1")
    assert viewer["viewer_id"] == "player-1"
    opening = engine.play_card(
        engine.state.active_player.id,
        engine.state.active_player.hand[0].id,
        *engine.legal_slots(engine.state.active_player.id)[0],
    )
    assert opening.accepted
    revealed_view = engine.view_for("player-1")
    first_slot = revealed_view["rows"][0]["slots"][0]
    assert first_slot["occupied"] is True
    if first_slot["card"] is None:
        pass  # face-down cards stay hidden with no public card data
    else:
        assert first_slot["card"]["id"]  # revealed cards disclose their identity

    # Out-of-turn and locked placements never reach the network.
    someone = "player-2"
    rejected = remote.play_card(someone, "card-x", "row-x", 0)
    assert not rejected.accepted
    assert connection.sent == []
    remote.remote_locked = True
    assert remote.legal_slots() == []

    # Reveals are acknowledged exactly once, even if the update repeats.
    push_update(connection, engine, "player-1", locked=True, events=[{
        "kind": "card_revealed", "message": "revealed",
        "data": {"row_id": "r", "slot_index": 0, "reveal_id": "reveal-9"},
    }])
    remote.poll()
    assert remote.reveal_next_card().accepted
    assert remote.reveal_next_card().accepted
    acks = [message for message in connection.sent if message.get("type") == "reveal_ack"]
    assert len(acks) == 1 and acks[0]["reveal_id"] == "reveal-9"

    # Only player-1 drives shared progress, sent once per round/phase; player-2 is a no-op.
    push_update(connection, engine, "player-1", locked=True)
    remote.poll()
    assert remote.continue_after_round_results().accepted
    assert remote.continue_after_round_results().accepted
    progress = [message for message in connection.sent if message.get("type") == "continue_round"]
    assert len(progress) == 1
    other = StubConnection(player_id="player-2", room_code="ABC123")
    push_update(other, engine, "player-2", locked=False)
    remote_two = RemoteGameEngine(other)
    remote_two.poll()
    assert remote_two.continue_after_round_results().accepted
    assert all(message.get("type") != "continue_round" for message in other.sent)

    #__PART4__


async def drive_match(room: Room, writers: dict[str, FakeWriter], limit: int = 4000) -> int:
    """Play a full match through the authoritative room as both peers.

    Placements always use the acting peer's own hand card and the room's
    advertised legal slot; both peers ack every pending reveal; player-1
    drives shared progress. Returns the step count.
    """
    steps = 0
    while steps < limit:
        steps += 1
        state = room.engine.state
        phase = state.phase
        if phase == GamePhase.MATCH_RESULTS:
            return steps
        if room.pending_reveal_id is not None:
            reveal_id = room.pending_reveal_id
            for player_id in ("player-1", "player-2"):
                await room.handle(
                    room.peers[player_id], {"type": "reveal_ack", "reveal_id": reveal_id},
                )
            continue
        if phase == GamePhase.PLACEMENT:
            active = state.active_player
            legal = room.engine.legal_slots(active.id)
            assert legal, "placement phase must advertise a legal slot"
            row_id, slot_index = legal[0]
            await room.handle(room.peers[active.id], {
                "type": "place",
                "card_id": active.hand[0].id,
                "row_id": row_id,
                "slot_index": slot_index,
            })
            continue
        if phase in {GamePhase.ROW_SCORING, GamePhase.ROW_RESULTS, GamePhase.ROUND_RESULTS}:
            action = {
                GamePhase.ROW_SCORING: "score",
                GamePhase.ROW_RESULTS: "advance_row",
                GamePhase.ROUND_RESULTS: "continue_round",
            }[phase]
            await room.handle(room.peers["player-1"], {"type": action})
            continue
        raise AssertionError(f"Unexpected phase while driving match: {phase}")
    raise AssertionError("Match did not finish within the step limit")


def test_two_peer_room_plays_a_full_match_to_results():
    room = Room("TEST12")
    writers = {"player-1": FakeWriter(), "player-2": FakeWriter()}
    asyncio.run(room.attach("player-1", writers["player-1"]))  # type: ignore[arg-type]
    assert not room.ready
    first_update = writers["player-1"].updates()[-1]
    assert first_update["locked"] is True
    assert first_update["view"]["legal_slots"] == []

    asyncio.run(room.attach("player-2", writers["player-2"]))  # type: ignore[arg-type]
    assert room.ready
    joined_p1 = [m for m in writers["player-1"].messages if m.get("type") == "joined"][0]
    joined_p2 = [m for m in writers["player-2"].messages if m.get("type") == "joined"][0]
    assert joined_p1["room_code"] == joined_p2["room_code"] == "TEST12"
    assert joined_p1["player_id"] == "player-1" and joined_p2["player_id"] == "player-2"

    notices = [m.get("notice") for m in writers["player-2"].messages if m.get("type") == "update"]
    assert "Both players are connected." in notices

    #__PART5__

    placement_count_before = room.engine.state.turn_number
    active = room.engine.state.active_player
    row_id, slot_index = room.engine.legal_slots(active.id)[0]

    async def send_first_placement():
        await room.handle(room.peers[active.id], {
            "type": "place",
            "card_id": active.hand[0].id,
            "row_id": row_id,
            "slot_index": slot_index,
        })

    asyncio.run(send_first_placement())
    assert room.engine.state.turn_number == placement_count_before + 1

    # Until both peers ack the reveal, further placement is refused server-side.
    if room.pending_reveal_id is not None:
        placement_count = room.engine.state.turn_number
        next_active = room.engine.state.active_player
        legal = room.engine.legal_slots(next_active.id)
        assert legal
        next_row, next_slot = legal[0]

        async def send_blocked_placement():
            await room.handle(room.peers[next_active.id], {
                "type": "place",
                "card_id": next_active.hand[0].id,
                "row_id": next_row,
                "slot_index": next_slot,
            })

        asyncio.run(send_blocked_placement())
        assert room.engine.state.turn_number == placement_count
        p2_errors = writers[next_active.id].errors()
        assert any("reveal" in error.get("message", "").lower() for error in p2_errors)

    steps = asyncio.run(drive_match(room, writers))
    assert room.engine.state.phase == GamePhase.MATCH_RESULTS
    assert room.engine.state.winner_id in {"player-1", "player-2", None}
    assert steps < 4000

    # A restart from player-1 opens a fresh match for both peers.
    async def send_restart():
        await room.handle(room.peers["player-1"], {"type": "restart"})

    asyncio.run(send_restart())
    assert room.engine.state.phase == GamePhase.PLACEMENT
    assert room.engine.state.round_number == 1
    assert room.engine.state.turn_number == 1
    final_updates = writers["player-1"].updates()
    assert any(
        update.get("notice") and "new match" in str(update.get("notice")).lower()
        for update in final_updates
    )