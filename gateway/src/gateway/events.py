"""In-process event bus behind `GET /api/v1/events` (Server-Sent Events).

Events carry ids, never content: the app refetches through the role-checked endpoints, so
the stream cannot leak guidance or another family's data. One gateway instance (MVP); a
second instance would need a shared broker such as Postgres LISTEN/NOTIFY.
"""

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gateway.enums import Role

logger = logging.getLogger(__name__)

QUEUE_SIZE = 100


class EventType(StrEnum):
    MESSAGE = "message"  # the agent replied on a request
    QUESTION = "question"  # the agent asked the guardian
    TRANSFER = "transfer"  # money moved, or a transfer settled
    NOTIFICATION = "notification"


@dataclass(frozen=True)
class Event:
    type: EventType
    data: dict[str, Any]
    audience: Role | None  # None: both members

    def encode(self) -> str:
        return f"event: {self.type.value}\ndata: {json.dumps(self.data)}\n\n"


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[tuple[Role, asyncio.Queue[Event]]]] = defaultdict(
            set
        )

    def subscribe(self, family_id: uuid.UUID, role: Role) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers[family_id].add((role, queue))
        return queue

    def unsubscribe(self, family_id: uuid.UUID, role: Role, queue: asyncio.Queue[Event]) -> None:
        self._subscribers[family_id].discard((role, queue))
        if not self._subscribers[family_id]:
            del self._subscribers[family_id]

    def publish(
        self,
        family_id: uuid.UUID,
        type: EventType,
        data: dict[str, Any],
        audience: Role | None = None,
    ) -> None:
        event = Event(type=type, data=data, audience=audience)
        for role, queue in list(self._subscribers.get(family_id, ())):
            if audience is not None and role != audience:
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client loses events, never the gateway's memory; it can refetch.
                logger.warning("Dropping event for a slow subscriber")
