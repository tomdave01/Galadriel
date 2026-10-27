"""Event Hubs clients and the in-process fallback feed."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol
from weakref import WeakKeyDictionary

from azure.eventhub import EventData
from azure.eventhub.aio import EventHubConsumerClient, EventHubProducerClient
from azure.identity.aio import DefaultAzureCredential

from feed.models import FeedMessage


logger = logging.getLogger(__name__)


class FeedProducer(Protocol):
    async def publish(self, message: FeedMessage) -> None: ...

    async def close(self) -> None: ...


class FeedConsumer(Protocol):
    async def receive(self) -> FeedMessage: ...

    async def close(self) -> None: ...


class InProcessQueue:
    """Single-consumer queue used only when Event Hubs is not configured."""

    def __init__(self) -> None:
        self._messages: asyncio.Queue[FeedMessage] = asyncio.Queue()

    async def publish(self, message: FeedMessage) -> None:
        await self._messages.put(message)

    async def receive(self) -> FeedMessage:
        return await self._messages.get()

    async def close(self) -> None:
        return None


class EventHubsProducer:
    def __init__(
        self,
        namespace: str,
        eventhub_name: str,
        *,
        client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self._credential = (
            credential if credential is not None else DefaultAzureCredential()
        )
        self._client = client if client is not None else EventHubProducerClient(
            fully_qualified_namespace=_fully_qualified_namespace(namespace),
            eventhub_name=eventhub_name,
            credential=self._credential,
        )

    async def publish(self, message: FeedMessage) -> None:
        batch = await self._client.create_batch(partition_key=message.match_id)
        batch.add(EventData(message.to_json()))
        await self._client.send_batch(batch)

    async def close(self) -> None:
        try:
            await self._client.close()
        finally:
            await self._credential.close()


class EventHubsConsumer:
    def __init__(
        self,
        namespace: str,
        eventhub_name: str,
        consumer_group: str,
        *,
        client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self._credential = (
            credential if credential is not None else DefaultAzureCredential()
        )
        self._client = client if client is not None else EventHubConsumerClient(
            fully_qualified_namespace=_fully_qualified_namespace(namespace),
            eventhub_name=eventhub_name,
            consumer_group=consumer_group,
            credential=self._credential,
        )
        self._messages: asyncio.Queue[FeedMessage | Exception] = asyncio.Queue()
        self._receive_task: asyncio.Task[None] | None = None
        self._failure: Exception | None = None

    async def _on_event(self, partition_context: Any, event: Any) -> None:
        if event is None:
            return
        try:
            message = FeedMessage.from_json(event.body_as_str(encoding="UTF-8"))
        except Exception as error:
            logger.exception("Unable to decode an Event Hubs feed message")
            self._failure = error
            await self._messages.put(error)
            raise
        logger.debug(
            "Received feed message from Event Hubs partition %s",
            partition_context.partition_id,
        )
        await self._messages.put(message)

    async def receive(self) -> FeedMessage:
        if self._receive_task is None:
            self._receive_task = asyncio.create_task(
                self._client.receive(
                    on_event=self._on_event,
                    starting_position="-1",
                    max_wait_time=5,
                )
            )
        if self._failure is not None:
            raise RuntimeError("Event Hubs consumer failed") from self._failure
        if self._receive_task.done():
            if self._receive_task.cancelled():
                raise RuntimeError("Event Hubs receiver was cancelled unexpectedly")
            error = self._receive_task.exception()
            if error is not None:
                raise RuntimeError("Event Hubs receiver stopped unexpectedly") from error
            raise RuntimeError("Event Hubs receiver stopped unexpectedly")

        message = await self._messages.get()
        if isinstance(message, Exception):
            raise RuntimeError("Event Hubs message decoding failed") from message
        return message

    async def close(self) -> None:
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        try:
            await self._client.close()
        finally:
            await self._credential.close()


_queues_by_loop: WeakKeyDictionary[asyncio.AbstractEventLoop, InProcessQueue] = (
    WeakKeyDictionary()
)


def _in_process_queue() -> InProcessQueue:
    loop = asyncio.get_running_loop()
    queue = _queues_by_loop.get(loop)
    if queue is None:
        queue = InProcessQueue()
        _queues_by_loop[loop] = queue
    return queue


def _fully_qualified_namespace(namespace: str) -> str:
    value = namespace.strip().rstrip("/")
    if not value:
        raise ValueError("EVENTHUB_NAMESPACE must not be blank")
    if "." not in value:
        return f"{value}.servicebus.windows.net"
    return value


def _eventhub_settings() -> tuple[str, str, str] | None:
    namespace = os.getenv("EVENTHUB_NAMESPACE", "").strip()
    if not namespace:
        return None
    return (
        namespace,
        os.getenv("EVENTHUB_NAME", "match-events").strip() or "match-events",
        os.getenv("EVENTHUB_CONSUMER_GROUP", "$Default").strip() or "$Default",
    )


def create_feed_producer() -> FeedProducer:
    settings = _eventhub_settings()
    if settings is None:
        logger.warning(
            "EVENTHUB_NAMESPACE is unset: using an in-process feed. "
            "Producer and processor must run in the same Python process; "
            "this fallback is not a distributed transport."
        )
        return _in_process_queue()
    namespace, eventhub_name, _ = settings
    return EventHubsProducer(namespace, eventhub_name)


def create_feed_consumer() -> FeedConsumer:
    settings = _eventhub_settings()
    if settings is None:
        logger.warning(
            "EVENTHUB_NAMESPACE is unset: using an in-process feed. "
            "Producer and processor must run in the same Python process; "
            "this fallback is not a distributed transport."
        )
        return _in_process_queue()
    namespace, eventhub_name, consumer_group = settings
    return EventHubsConsumer(namespace, eventhub_name, consumer_group)
