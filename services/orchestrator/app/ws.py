"""Live incident feed over WebSocket (PLAN.md: "REST API plus WebSocket / SSE for the live
incident feed"). A thread-safe broadcaster: `publish` is called from the Kafka-consumer and
sweeper threads (via state_machine.Orchestrator's broadcast callback), while connections are
only ever touched from the FastAPI event loop thread. `asyncio.Queue` per connection makes that
handoff safe without either side needing a lock around the send itself.
"""

import asyncio
import logging

from fastapi import WebSocket

log = logging.getLogger("orchestrator.ws")


class ConnectionManager:
    def __init__(self):
        self._connections: dict[WebSocket, asyncio.Queue] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> asyncio.Queue:
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue()
        self._connections[websocket] = queue
        return queue

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    def publish(self, event: dict) -> None:
        """Called from any thread. Hands the event to each connection's queue on the event
        loop thread, so no cross-thread mutation of asyncio state ever happens directly."""
        if self._loop is None:
            return
        for queue in list(self._connections.values()):
            self._loop.call_soon_threadsafe(queue.put_nowait, event)
