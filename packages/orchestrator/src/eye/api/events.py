"""Fan out live events to independent SSE subscribers."""

import asyncio
from collections.abc import AsyncIterator

from eye.contracts_v2 import Event


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def publish(self, event: Event) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    async def stream(self, heartbeat_seconds: float = 15) -> AsyncIterator[str]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + heartbeat_seconds
        try:
            while True:
                if loop.time() >= next_heartbeat:
                    yield ": heartbeat\n\n"
                    next_heartbeat = loop.time() + heartbeat_seconds
                try:
                    async with asyncio.timeout_at(next_heartbeat):
                        event = await queue.get()
                except TimeoutError:
                    continue
                yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
        finally:
            self._subscribers.remove(queue)
