"""Fan out live events to independent SSE subscribers."""

import asyncio
from collections.abc import AsyncIterator

from eye.contracts import Event


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
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), heartbeat_seconds)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                else:
                    yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
        finally:
            self._subscribers.remove(queue)
