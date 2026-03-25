from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("localmelo.gateway.channel")


class Channel(ABC):
    """Base class for channel adapters.

    A channel receives external events (iMessage, HTTP webhook, etc.)
    and forwards them to the gateway for processing.
    """

    name: str = "base"

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @abstractmethod
    async def start(self, on_message: MessageCallback) -> None:
        """Start listening. Call on_message(channel, sender, text) for each event."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening and clean up resources."""

    @abstractmethod
    async def send(self, recipient: str, text: str) -> None:
        """Send a reply back through this channel."""

    @property
    def is_running(self) -> bool:
        return self._running


# callback: (channel_name, sender_id, message_text) -> response text
MessageCallback = Any  # Callable[[str, str, str], Awaitable[str]]
