"""Staying alive in the Duels.ink matchmaking queue.

Joining a queue is not enough to be matched. The real client does three
things at once, and only the first is a single request:

    POST /api/matchmaking/join       enter the queue
    GET  /api/matchmaking/events     a Server-Sent Events stream
    POST /api/matchmaking/heartbeat  every second or so, forever

The heartbeat is what makes the entry real. Without it the server keeps
returning a queue position - the join looks like it worked, and calling it
again even reports a plausible-looking `position` - but no opponent is ever
paired with you. Watching the site do it was the only way to find this: 21
minutes were spent queued against a live queue with players in it, believing
it was empty.

Two further traps found the same way:

* Re-POSTing `join` is not a way to poll. It re-enters the queue, which moves
  you to the back and can undo a pairing already in progress.
* On a match the client POSTs `leave` itself and then opens the game. Leaving
  is part of the success path, not only of giving up.

The events stream is not consumed here. Once the heartbeat is running, the
match shows up as a game in /api/account/active-games within a second or two,
which needs no SSE client and survives the stream dropping - it was observed
returning 503 mid-queue while the queue itself stayed healthy.
"""

import asyncio
import contextlib
import logging
from typing import Any, Optional

from .client import DuelsClient, DuelsError

log = logging.getLogger("duels_mcp.matchmaking")

# The site sends one roughly every 1.5s. A little under that leaves room for a
# slow round trip without the entry ever looking stale.
HEARTBEAT_SECONDS = 1.2

# How often to ask whether the pairing has become a game.
POLL_SECONDS = 1.0


class Matchmaker:
    """Owns the heartbeat that keeps one queue entry alive."""

    def __init__(self, client: DuelsClient) -> None:
        self._client = client
        self._task: Optional[asyncio.Task] = None
        self.queue_id: Optional[str] = None
        self.deck_id: Optional[str] = None
        self.beats = 0
        self.last_error: Optional[str] = None

    @property
    def queued(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _beat(self) -> None:
        """Heartbeat until cancelled.

        A failed beat is logged and retried rather than ending the loop: a
        single dropped request should not silently take you out of the queue,
        which is the exact failure this class exists to prevent.
        """
        while True:
            try:
                await self._client.post("/api/matchmaking/heartbeat", {})
                self.beats += 1
                self.last_error = None
            except DuelsError as exc:
                self.last_error = str(exc)
                log.warning("matchmaking heartbeat failed: %s", exc)
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(HEARTBEAT_SECONDS)

    def start(self, queue_id: str, deck_id: Optional[str]) -> None:
        """Begin heartbeating for a queue entry that was just joined."""
        self.stop()
        self.queue_id = queue_id
        self.deck_id = deck_id
        self.beats = 0
        self.last_error = None
        self._task = asyncio.create_task(self._beat())

    def stop(self) -> None:
        """Stop heartbeating. Safe to call when not queued."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.queue_id = None

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def active_game_id(self) -> Optional[str]:
        """The id of a game we are now in, if the pairing has landed."""
        try:
            data = await self._client.get("/api/account/active-games")
        except DuelsError:
            return None
        games = (data or {}).get("games") or []
        for game in games:
            game_id = game.get("id")
            if game_id:
                return str(game_id)
        return None

    async def wait_for_match(self, timeout_seconds: float) -> dict[str, Any]:
        """Block until the queue produces a game, or the timeout expires.

        Returns {"game_id": ...} on a match and {"timed_out": True} otherwise.
        Timing out is not an error - the queue is still live and the caller can
        simply wait again, exactly as duels_wait_for_my_turn behaves.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            game_id = await self.active_game_id()
            if game_id:
                # The site leaves the queue itself once matched; do the same so
                # a stale entry cannot pair us again mid-game.
                with contextlib.suppress(DuelsError):
                    await self._client.post("/api/matchmaking/leave", {})
                self.stop()
                return {"game_id": game_id, "beats": self.beats}
            await asyncio.sleep(POLL_SECONDS)
        return {
            "timed_out": True,
            "queue_id": self.queue_id,
            "beats": self.beats,
            "last_error": self.last_error,
        }


__all__ = ["Matchmaker", "HEARTBEAT_SECONDS"]
