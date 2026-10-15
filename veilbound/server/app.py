from __future__ import annotations

import argparse
import asyncio
import logging

from veilbound.shared.protocol import MAX_MESSAGE_BYTES
from .rooms import RoomManager


async def serve(host: str, port: int) -> None:
    manager = RoomManager()
    server = await asyncio.start_server(
        manager.accept, host, port, limit=MAX_MESSAGE_BYTES + 1,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logging.getLogger(__name__).info("Veilbound server listening on %s", addresses)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Veilbound multiplayer server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for LAN play)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        pass
