"""Per-bot batch isolation for concurrent Telegram source bots."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class BotBatch:
    bot_username: str
    token: str
    start_episode: int
    end_episode: int

    @property
    def expected_count(self) -> int:
        return self.end_episode - self.start_episode + 1


class BotBatchCoordinator:
    """Serialize requests per bot while allowing different bots concurrently.

    The caller supplies a Telegram client adapter with get_input_entity,
    iter_messages, and send_message methods.
    """

    def __init__(self, client: Any, *, poll_seconds: float = 2.0) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, username: str) -> asyncio.Lock:
        return self._locks.setdefault(username.lower(), asyncio.Lock())

    @staticmethod
    def _episode_number(message: Any) -> int | None:
        values: list[str] = []
        document = getattr(getattr(message, "media", None), "document", None)
        for attribute in getattr(document, "attributes", ()) or ():
            for field in ("file_name", "title"):
                value = getattr(attribute, field, None)
                if value:
                    values.append(str(value))
        for value in values:
            match = re.search(r"(?:ep|episode|chapter)\s*[-_: ]*([0-9]+)", value, re.I)
            if match:
                return int(match.group(1))
            match = re.search(r"\b([0-9]+)\b", value)
            if match:
                return int(match.group(1))
        return None

    async def request(
        self,
        batch: BotBatch,
        *,
        retries: int = 3,
        on_retry: Callable[[BotBatch, int], Awaitable[None]] | None = None,
    ) -> list[Any]:
        lock = self._lock_for(batch.bot_username)
        async with lock:
            entity = await self.client.get_input_entity(batch.bot_username)
            for attempt in range(1, retries + 1):
                baseline = 0
                async for message in self.client.iter_messages(entity, limit=1):
                    baseline = message.id
                    break
                await self.client.send_message(entity, f"/start {batch.token}")
                for _ in range(40):
                    await asyncio.sleep(self.poll_seconds)
                    received: list[Any] = []
                    async for message in self.client.iter_messages(entity, limit=100):
                        if message.id <= baseline:
                            break
                        if getattr(getattr(message, "media", None), "document", None):
                            received.append(message)
                    received.reverse()
                    numbers = [self._episode_number(message) for message in received]
                    expected = list(range(batch.start_episode, batch.end_episode + 1))
                    if len(received) == batch.expected_count and numbers == expected:
                        return received
                if on_retry is not None:
                    await on_retry(batch, attempt)
            raise RuntimeError(
                f"bot @{batch.bot_username} did not return the exact "
                f"{batch.start_episode}-{batch.end_episode} batch"
            )

    async def request_many(self, batches: list[BotBatch]) -> dict[str, list[Any]]:
        results = await asyncio.gather(
            *(self.request(batch) for batch in batches),
            return_exceptions=True,
        )
        output: dict[str, list[Any]] = {}
        failures: list[str] = []
        for batch, result in zip(batches, results):
            if isinstance(result, Exception):
                failures.append(f"{batch.bot_username}:{batch.start_episode}-{batch.end_episode}: {result}")
            else:
                output[f"{batch.bot_username}:{batch.start_episode}"] = result
        if failures:
            raise RuntimeError("; ".join(failures))
        return output
