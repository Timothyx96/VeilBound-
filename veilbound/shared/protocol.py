"""JSON Lines messages shared by the online client and room server."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


MAX_MESSAGE_BYTES = 1_048_576


def encode_message(message: Mapping[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_message(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("Message is too large")
    message = json.loads(data.decode("utf-8"))
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ValueError("A protocol message must be an object with a type")
    return message
