from __future__ import annotations

import queue
import select
import socket
import threading
import time
from typing import Any

from veilbound.shared.definitions import load_card_definitions
from veilbound.shared.models import (
    ActionResult, BattleRow, CardInstance, GameEvent, GamePhase, GameState, PlayerState,
)
from veilbound.shared.protocol import decode_message, encode_message


class NetworkConnection:
    """Small reconnecting socket worker; Pygame only talks to its thread-safe queues."""

    def __init__(self, host: str, port: int, mode: str, room_code: str | None = None):
        self.host = host
        self.port = port
        self.mode = mode
        self.room_code = room_code
        self.player_id: str | None = None
        self.token: str | None = None
        self.outgoing: queue.Queue[dict] = queue.Queue()
        self.incoming: queue.Queue[dict] = queue.Queue()
        self.connected = threading.Event()
        self.failed = threading.Event()
        self.stop_requested = threading.Event()
        self._socket: socket.socket | None = None
        self._thread = threading.Thread(target=self._run, name="veilbound-network", daemon=True)
        self._thread.start()

    def wait_until_joined(self, timeout: float = 12.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected.wait(0.1):
                return True
            if self.failed.is_set():
                return False
        return False

    def send(self, message: dict) -> None:
        self.outgoing.put(message)

    def close(self) -> None:
        self.stop_requested.set()
        current_socket = self._socket
        if current_socket is not None:
            try:
                current_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current_socket.close()
            except OSError:
                pass
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        first_attempt = True
        while not self.stop_requested.is_set():
            try:
                current_socket = socket.create_connection((self.host, self.port), timeout=4)
                current_socket.setblocking(False)
                self._socket = current_socket
                hello = self._hello(first_attempt)
                current_socket.sendall(encode_message(hello))
                receive_buffer = bytearray()
                while not self.stop_requested.is_set():
                    wants_write = not self.outgoing.empty()
                    readable, writable, _ = select.select(
                        [current_socket], [current_socket] if wants_write else [], [], 0.1,
                    )
                    if readable:
                        chunk = current_socket.recv(65536)
                        if not chunk:
                            raise ConnectionError("Server closed the connection")
                        receive_buffer.extend(chunk)
                        while b"\n" in receive_buffer:
                            line, _, remainder = receive_buffer.partition(b"\n")
                            receive_buffer = bytearray(remainder)
                            self._receive(decode_message(bytes(line)))
                            if self.failed.is_set():
                                return
                    if writable:
                        try:
                            message = self.outgoing.get_nowait()
                        except queue.Empty:
                            continue
                        current_socket.sendall(encode_message(message))
                break
            except (OSError, ConnectionError, ValueError) as error:
                if first_attempt and not self.connected.is_set():
                    self.failed.set()
                    self.incoming.put({"type": "error", "message": f"Could not connect: {error}"})
                    return
                self.connected.clear()
                self.incoming.put({"type": "disconnected", "message": "Disconnected. Trying to reconnect…"})
            finally:
                current_socket = self._socket
                self._socket = None
                if current_socket is not None:
                    try:
                        current_socket.close()
                    except OSError:
                        pass
            if first_attempt and not self.connected.is_set() and self.failed.is_set():
                return
            first_attempt = False
            self.stop_requested.wait(1.0)

    def _hello(self, first_attempt: bool) -> dict:
        if not first_attempt and self.player_id and self.token and self.room_code:
            return {
                "type": "hello", "mode": "reconnect", "room_code": self.room_code,
                "player_id": self.player_id, "token": self.token,
            }
        message: dict[str, Any] = {"type": "hello", "mode": self.mode}
        if self.room_code:
            message["room_code"] = self.room_code
        return message

    def _receive(self, message: dict) -> None:
        if message.get("type") == "joined":
            self.room_code = message.get("room_code", self.room_code)
            self.player_id = message.get("player_id", self.player_id)
            self.token = message.get("token", self.token)
            self.connected.set()
        elif message.get("type") == "error" and not self.connected.is_set():
            self.failed.set()
        self.incoming.put(message)


class RemoteGameEngine:
    """Client-side projection and action sender; it never computes game rules."""

    is_remote = True

    def __init__(self, connection: NetworkConnection):
        self.connection = connection
        self.cards = load_card_definitions()
        self.state: GameState | None = None
        self._legal_slots: list[tuple[str, int]] = []
        self.remote_locked = True
        self.room_code = connection.room_code
        self.player_id = connection.player_id
        self._events: list[GameEvent] = []
        self._current_reveal_id: str | None = None
        self._sent_requests: set[tuple[str, str]] = set()
        self.network_notice: str | None = None
        self._last_progress_state: tuple[int, str, int, int] | None = None

    def poll(self) -> None:
        while True:
            try:
                message = self.connection.incoming.get_nowait()
            except queue.Empty:
                break
            kind = message.get("type")
            if kind == "joined":
                self.room_code = message.get("room_code", self.room_code)
                self.player_id = message.get("player_id", self.player_id)
            elif kind == "update":
                self.state = _state_from_view(message["view"])
                progress_state = (
                    self.state.round_number, self.state.phase.value,
                    self.state.row_index, self.state.turn_number,
                )
                if self._last_progress_state is not None and progress_state != self._last_progress_state:
                    self._sent_requests.clear()
                self._last_progress_state = progress_state
                self.player_id = self.state.viewer_id
                self.remote_locked = bool(message.get("locked", False))
                self._legal_slots = [tuple(item) for item in message["view"].get("legal_slots", [])]
                if message.get("notice"):
                    self.network_notice = str(message["notice"])
                for raw_event in message.get("events", []):
                    event = GameEvent(raw_event["kind"], raw_event.get("message", ""), dict(raw_event.get("data", {})))
                    self._events.append(event)
                    if event.kind == "card_revealed":
                        self._current_reveal_id = event.data.get("reveal_id")
            elif kind in {"error", "disconnected"}:
                self.network_notice = str(message.get("message", "Connection issue."))
                if kind == "disconnected":
                    self.remote_locked = True

    def drain_events(self) -> tuple[GameEvent, ...]:
        result = tuple(self._events)
        self._events.clear()
        return result

    def drain_notice(self) -> str | None:
        notice, self.network_notice = self.network_notice, None
        return notice

    def legal_slots(self, player_id: str | None = None) -> list[tuple[str, int]]:
        if self.remote_locked or self.state is None or player_id not in (None, self.player_id):
            return []
        return list(self._legal_slots)

    def play_card(self, player_id: str, card_id: str, row_id: str, slot_index: int) -> ActionResult:
        if self.remote_locked or player_id != self.player_id:
            return ActionResult(False, "Wait for your turn and the current reveal to finish.")
        self.connection.send({
            "type": "place", "card_id": card_id, "row_id": row_id, "slot_index": slot_index,
        })
        return ActionResult(True)

    def reveal_next_card(self) -> ActionResult:
        if self._current_reveal_id is None:
            return ActionResult(False, "There is no reveal waiting to finish.")
        request = ("reveal_ack", self._current_reveal_id)
        if request not in self._sent_requests:
            self._sent_requests.add(request)
            self.connection.send({"type": "reveal_ack", "reveal_id": self._current_reveal_id})
        return ActionResult(True)

    def score_current_row(self) -> ActionResult:
        return self._send_progress("score")

    def advance_after_row_result(self) -> ActionResult:
        return self._send_progress("advance_row")

    def continue_after_round_results(self) -> ActionResult:
        return self._send_progress("continue_round")

    def start_new_match(self) -> None:
        self._send_progress("restart")

    def _send_progress(self, action: str) -> ActionResult:
        if self.player_id != "player-1":
            return ActionResult(True)
        request = (action, str(self.state.round_number if self.state else 0) + ":" + str(self.state.phase.value if self.state else ""))
        if request not in self._sent_requests:
            self._sent_requests.add(request)
            self.connection.send({"type": action})
        return ActionResult(True)


def _state_from_view(view: dict) -> GameState:
    viewer_id = str(view["viewer_id"])
    players = []
    for player_view in view["players"]:
        hand = [
            CardInstance(str(card["id"]), str(card["definition_id"]), str(player_view["id"]))
            for card in (player_view.get("hand") or [])
        ]
        draw_pile = [
            CardInstance(f"hidden-draw-{player_view['id']}-{index}", "", str(player_view["id"]))
            for index in range(int(player_view.get("draw_pile_count", 0)))
        ]
        used = [
            CardInstance(f"hidden-used-{player_view['id']}-{index}", "", str(player_view["id"]))
            for index in range(int(player_view.get("used_count", 0)))
        ]
        players.append(PlayerState(
            id=str(player_view["id"]), name=str(player_view["name"]), draw_pile=draw_pile,
            hand=hand, used=used, match_points=int(player_view["match_points"]),
            round_points=int(player_view["round_points"]),
        ))

    rows = []
    for row_view in view["rows"]:
        slots = []
        for slot_index, slot_view in enumerate(row_view["slots"]):
            if not slot_view.get("occupied"):
                slots.append(None)
                continue
            card_view = slot_view.get("card")
            if card_view is None:
                slots.append(CardInstance(
                    f"hidden-{row_view['id']}-{slot_index}", "", str(slot_view["owner_id"]),
                ))
            else:
                slots.append(CardInstance(
                    str(card_view["id"]), str(card_view["definition_id"]),
                    str(card_view["owner_id"]), revealed=True,
                    # Runtime ability state mirrors the authority's view.
                    current_points=card_view.get("current_points"),
                    activated_abilities=list(card_view.get("activated_abilities") or []),
                ))
        rows.append(BattleRow(
            id=str(row_view["id"]), number=int(row_view["number"]),
            capacity=int(row_view["capacity"]), point_value=int(row_view.get("point_value", row_view["capacity"])),
            slots=slots, scores=dict(row_view.get("scores") or {}), winner_id=row_view.get("winner_id"),
            scored=bool(row_view["scored"]), awarded_points=int(row_view["awarded_points"]),
        ))

    player_ids = [player.id for player in players]
    active_player_id = str(view["active_player_id"])
    current_row_id = view.get("current_row_id")
    row_index = next((index for index, row in enumerate(rows) if row.id == current_row_id), 0)
    return GameState(
        players=players, rows=rows, rounds_per_match=int(view["rounds_per_match"]),
        active_player_index=player_ids.index(active_player_id), turn_number=int(view["turn_number"]),
        round_number=int(view["round_number"]), phase=GamePhase(view["phase"]), row_index=row_index,
        final_reveal_active=GamePhase(view["phase"]) == GamePhase.REVEAL,
        winner_id=view.get("winner_id"), scored_row_order=list(view.get("scored_row_order", [])),
        viewer_id=viewer_id,
    )
