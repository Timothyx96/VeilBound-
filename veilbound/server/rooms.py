from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass, field

from veilbound.shared.engine import GameEngine
from veilbound.shared.models import ActionResult, GameEvent, GamePhase
from veilbound.shared.protocol import MAX_MESSAGE_BYTES, decode_message, encode_message


ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class Peer:
    player_id: str
    token: str
    writer: asyncio.StreamWriter | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def send(self, message: dict) -> None:
        writer = self.writer
        if writer is None or writer.is_closing():
            return
        async with self.send_lock:
            writer.write(encode_message(message))
            await writer.drain()


class Room:
    def __init__(self, code: str):
        self.code = code
        self.engine = GameEngine()
        self.peers = {
            "player-1": Peer("player-1", secrets.token_urlsafe(24)),
            "player-2": Peer("player-2", secrets.token_urlsafe(24)),
        }
        self.pending_reveal_id: str | None = None
        self.pending_reveal_event: dict | None = None
        self.reveal_acks: set[str] = set()
        self.reveal_sequence = 0
        self.player_two_joined = False

    @property
    def ready(self) -> bool:
        return all(peer.connected for peer in self.peers.values())

    async def attach(self, player_id: str, writer: asyncio.StreamWriter) -> Peer:
        peer = self.peers[player_id]
        peer.writer = writer
        await peer.send({
            "type": "joined",
            "room_code": self.code,
            "player_id": player_id,
            "token": peer.token,
        })
        await self._send_update(
            peer, [],
            notice=None if self.ready else "Room created. Waiting for Player 2 to join.",
            replay_pending=True,
        )
        if self.ready:
            await self.broadcast([], notice="Both players are connected.")
        return peer

    async def detach(self, peer: Peer, writer: asyncio.StreamWriter) -> None:
        if peer.writer is not writer:
            return
        peer.writer = None
        for other in self.peers.values():
            if other.connected:
                await self._send_update(other, [], notice=f"{peer.player_id} disconnected. The match is paused until they reconnect.")

    async def handle(self, peer: Peer, message: dict) -> None:
        message_type = message["type"]
        if message_type == "place":
            await self._place(peer, message)
        elif message_type == "reveal_ack":
            await self._reveal_ack(peer, message)
        elif message_type in {"score", "advance_row", "continue_round", "restart"}:
            await self._progress(peer, message_type)
        else:
            await peer.send({"type": "error", "message": "Unknown action."})

    async def _place(self, peer: Peer, message: dict) -> None:
        if not self.ready:
            await peer.send({"type": "error", "message": "Waiting for the other player to reconnect."})
            return
        if self.pending_reveal_id is not None:
            await peer.send({"type": "error", "message": "Wait for the current reveal to finish."})
            return
        try:
            slot_index = message.get("slot_index")
            result = self.engine.play_card(
                peer.player_id,
                str(message.get("card_id", "")),
                str(message.get("row_id", "")),
                slot_index,
            )
        except (TypeError, ValueError):
            await peer.send({"type": "error", "message": "Invalid card placement."})
            return
        if not result.accepted:
            await peer.send({"type": "error", "message": result.reason})
            return
        await self._publish_result(result)
        if self.engine.state.phase == GamePhase.REVEAL and self.pending_reveal_id is None:
            await self._start_next_final_reveal()

    async def _progress(self, peer: Peer, message_type: str) -> None:
        if not self.ready:
            await peer.send({"type": "error", "message": "Waiting for the other player to reconnect."})
            return
        # One consistent coordinator advances presentation phases for the room.
        if peer.player_id != "player-1":
            return
        if self.pending_reveal_id is not None:
            return
        if message_type == "score":
            result = self.engine.score_current_row()
        elif message_type == "advance_row":
            result = self.engine.advance_after_row_result()
        elif message_type == "continue_round":
            result = self.engine.continue_after_round_results()
        else:
            if self.engine.state.phase != GamePhase.MATCH_RESULTS:
                return
            self.engine.start_new_match()
            await self.broadcast([], notice="A new match has started.")
            return
        if result.accepted:
            await self._publish_result(result)

    async def _reveal_ack(self, peer: Peer, message: dict) -> None:
        if not self.ready or self.pending_reveal_id is None:
            return
        if message.get("reveal_id") != self.pending_reveal_id:
            return
        self.reveal_acks.add(peer.player_id)
        if self.reveal_acks != set(self.peers):
            return
        self.pending_reveal_id = None
        self.pending_reveal_event = None
        self.reveal_acks.clear()
        if self.engine.state.phase == GamePhase.REVEAL:
            await self._start_next_final_reveal()
        else:
            await self.broadcast([])

    async def _start_next_final_reveal(self) -> None:
        if not self.ready or self.pending_reveal_id is not None:
            return
        result = self.engine.reveal_next_card()
        if result.accepted:
            await self._publish_result(result)
        else:
            await self.broadcast([])

    async def _publish_result(self, result: ActionResult) -> None:
        outgoing = []
        for event in result.events:
            data = dict(event.data)
            if event.kind == "card_revealed":
                self.reveal_sequence += 1
                reveal_id = f"{self.engine.state.round_number}:{self.reveal_sequence}"
                data["reveal_id"] = reveal_id
                self.pending_reveal_id = reveal_id
                self.pending_reveal_event = {"kind": event.kind, "message": event.message, "data": data}
            outgoing.append({"kind": event.kind, "message": event.message, "data": data})
        await self.broadcast(outgoing)

    async def broadcast(self, events: list[dict], notice: str | None = None) -> None:
        for peer in self.peers.values():
            if peer.connected:
                await self._send_update(peer, events, notice)

    async def _send_update(
        self,
        peer: Peer,
        events: list[dict],
        notice: str | None = None,
        replay_pending: bool = False,
    ) -> None:
        view = self.engine.view_for(peer.player_id)
        view["legal_slots"] = view["legal_slots"] if self.ready and self.pending_reveal_id is None else []
        message = {
            "type": "update",
            "view": view,
            "events": events,
            "locked": not self.ready or self.pending_reveal_id is not None,
            "pending_reveal_id": self.pending_reveal_id,
        }
        if notice:
            message["notice"] = notice
        # A reconnecting peer needs to resume the reveal presentation it may have missed.
        if replay_pending and self.pending_reveal_event is not None and peer.player_id not in self.reveal_acks:
            message["events"] = [self.pending_reveal_event]
        await peer.send(message)


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer: Peer | None = None
        room: Room | None = None
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not first_line or len(first_line) > MAX_MESSAGE_BYTES:
                return
            hello = decode_message(first_line)
            if hello["type"] != "hello":
                await self._send_error(writer, "Send hello before game actions.")
                return
            try:
                room, peer = self._connect(hello)
            except ValueError as error:
                await self._send_error(writer, str(error))
                return
            await room.attach(peer.player_id, writer)
            while line := await reader.readline():
                if len(line) > MAX_MESSAGE_BYTES:
                    await self._send_error(writer, "Message is too large.")
                    break
                try:
                    await room.handle(peer, decode_message(line))
                except (ValueError, TypeError, KeyError):
                    await self._send_error(writer, "Invalid game action.")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            if room is not None and peer is not None:
                await room.detach(peer, writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def _connect(self, hello: dict) -> tuple[Room, Peer]:
        mode = hello.get("mode")
        if mode == "create":
            while True:
                code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))
                if code not in self.rooms:
                    break
            room = Room(code)
            self.rooms[code] = room
            return room, room.peers["player-1"]
        code = str(hello.get("room_code", "")).upper()
        room = self.rooms.get(code)
        if room is None:
            raise ValueError("Room code not found.")
        if mode == "join":
            peer = room.peers["player-2"]
            if peer.connected or any(p.token == peer.token and p.writer is not None for p in room.peers.values()):
                raise ValueError("That room already has two players.")
            # Player 2 is assignable only before their private reconnect token is disclosed.
            if hello.get("player_id") or hello.get("token"):
                raise ValueError("Use reconnect for an existing player.")
            if getattr(room, "player_two_joined", False):
                raise ValueError("That room already has two players.")
            room.player_two_joined = True
            return room, peer
        if mode == "reconnect":
            player_id = str(hello.get("player_id", ""))
            peer = room.peers.get(player_id)
            if peer is None or hello.get("token") != peer.token:
                raise ValueError("Reconnect credentials were not accepted.")
            if peer.connected:
                raise ValueError("That player is already connected.")
            return room, peer
        raise ValueError("Choose create, join, or reconnect.")

    @staticmethod
    async def _send_error(writer: asyncio.StreamWriter, message: str) -> None:
        writer.write(encode_message({"type": "error", "message": message}))
        try:
            await writer.drain()
        except ConnectionError:
            pass
