"""Getting through the Duels.ink matchmaking queue and into a game.

Joining a queue is one request out of three, and the other two are what
actually produce an opponent:

    POST /api/matchmaking/join       enter the queue
    GET  /api/matchmaking/events     Server-Sent Events, pushes the pairing
    POST /api/matchmaking/heartbeat  about once a second, for as long as you wait

Then the pairing itself has to be accepted, inside a window:

    match_found   {matchId, expiresAt}   -> about 15 seconds to answer
    POST /api/matchmaking/accept {matchId}
    match_accepted, then game_starting {gameId}

None of this is visible from the API alone. Without the heartbeat, `join`
still returns a position and asking again still reports one, so an entry that
will never be matched looks exactly like a healthy one in an empty queue.
Twenty-one minutes were spent that way against queues that had players in
them. Watching the site do it was the only way to find out, and the recording
also showed an `opponent_ready` arriving for a pairing nobody on this side
answered - a real person had accepted and was left waiting.

Two further traps found the same way:

* Re-POSTing `join` is not a way to poll. It re-enters the queue, which moves
  you to the back and can undo a pairing already in progress.
* On a match the client POSTs `leave` itself and then opens the game. Leaving
  is part of the success path, not only of giving up.

The events stream is the fast path and `/api/account/active-games` is the
fallback, because the stream was seen returning 503 mid-queue while the queue
itself stayed healthy.
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

# Fallback poll, for when the events stream has dropped.
POLL_SECONDS = 1.0


class Matchmaker:
    """Keeps one queue entry alive, accepts the pairing, and reports the game."""

    def __init__(self, client: DuelsClient) -> None:
        self._client = client
        self._tasks: list[asyncio.Task] = []
        self._matched = asyncio.Event()
        self.queue_id: Optional[str] = None
        self.deck_id: Optional[str] = None
        self.game_id: Optional[str] = None
        self.match_id: Optional[str] = None
        self.beats = 0
        self.accepted = False
        self._error: Optional[str] = None
        self._beat_error: Optional[str] = None

    @property
    def queued(self) -> bool:
        return any(not task.done() for task in self._tasks)

    @property
    def last_error(self) -> Optional[str]:
        """Why the wait is not getting anywhere, if anything.

        A refused accept or a dropped stream sticks until the next join,
        because it explains a pairing that was missed and is the one thing
        worth reading after a timeout. A failed beat clears as soon as one
        succeeds - the loop retries, so a single dropped request says nothing.

        Keeping these apart matters: a beat lands every 1.2s, so clearing on
        success would wipe a refused accept long before the caller returned.
        """
        return self._error or self._beat_error

    # -----------------------------------------------------------------
    # The two background jobs
    # -----------------------------------------------------------------
    async def _heartbeat(self) -> None:
        """Beat until cancelled.

        A failed beat is logged and retried rather than ending the loop: one
        dropped request must not silently take us out of the queue, which is
        the exact failure this class exists to prevent.
        """
        while True:
            try:
                await self._client.post("/api/matchmaking/heartbeat", {})
                self.beats += 1
                self._beat_error = None
            except DuelsError as exc:
                self._beat_error = str(exc)
                log.warning("matchmaking heartbeat failed: %s", exc)
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _listen(self) -> None:
        """Follow the events stream, accepting a pairing the moment it lands.

        The acceptance window is short - about fifteen seconds - and there is a
        person on the other side already waiting, so this answers immediately
        rather than handing the decision back to the caller.
        """
        try:
            async with self._client.stream_events("/api/matchmaking/events") as events:
                async for event in events:
                    kind = event.get("type")
                    data = event.get("data") or {}

                    if kind == "match_found":
                        await self._accept(data.get("matchId"))
                    elif kind == "game_starting" and data.get("gameId"):
                        self.game_id = str(data["gameId"])
                        self._matched.set()
                        return
                    elif data.get("activeGameId"):
                        # `init` carries it when we reconnect to a queue that
                        # has already produced a game.
                        self.game_id = str(data["activeGameId"])
                        self._matched.set()
                        return
                    elif kind == "match_cancelled":
                        log.info("matchmaking: pairing cancelled, still queued")
                        self.match_id = None
                        self.accepted = False
        except asyncio.CancelledError:
            raise
        except DuelsError as exc:
            # The stream has been seen returning 503 while the queue stayed
            # healthy, so this is not fatal - wait_for_match keeps polling.
            self._error = str(exc)
            log.warning("matchmaking event stream ended: %s", exc)
        except Exception as exc:  # pragma: no cover - transport surprises
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("matchmaking event stream failed: %s", exc)

    async def _accept(self, match_id: Optional[str]) -> None:
        if not match_id:
            return
        self.match_id = str(match_id)
        try:
            await self._client.post("/api/matchmaking/accept", {"matchId": self.match_id})
            self.accepted = True
            log.info("matchmaking: accepted %s", self.match_id)
        except DuelsError as exc:
            self._error = str(exc)
            log.warning("matchmaking accept failed for %s: %s", self.match_id, exc)

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def start(self, queue_id: str, deck_id: Optional[str]) -> None:
        """Begin heartbeating and listening for a queue entry just joined."""
        self.stop()
        self.queue_id = queue_id
        self.deck_id = deck_id
        self.game_id = None
        self.match_id = None
        self.beats = 0
        self.accepted = False
        self._error = None
        self._beat_error = None
        self._matched = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._listen()),
        ]

    def stop(self) -> None:
        """Stop the background jobs. Safe to call when not queued."""
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        self.queue_id = None

    async def close(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -----------------------------------------------------------------
    # Waiting
    # -----------------------------------------------------------------
    async def _active_game_id(self) -> Optional[str]:
        try:
            data = await self._client.get("/api/account/active-games")
        except DuelsError:
            return None
        for game in (data or {}).get("games") or []:
            if game.get("id"):
                return str(game["id"])
        return None

    async def wait_for_match(self, timeout_seconds: float) -> dict[str, Any]:
        """Block until the queue produces a game, or the timeout expires.

        Returns {"game_id": ...} on a match and {"timed_out": True} otherwise.
        Timing out is not an error - the queue is still live and the caller can
        wait again, exactly as duels_wait_for_my_turn behaves.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds

        while loop.time() < deadline:
            remaining = min(POLL_SECONDS, max(0.0, deadline - loop.time()))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._matched.wait(), timeout=remaining)

            game_id = self.game_id or await self._active_game_id()
            if game_id:
                # The site leaves the queue itself once matched; do the same so
                # a stale entry cannot pair us again mid-game.
                with contextlib.suppress(DuelsError):
                    await self._client.post("/api/matchmaking/leave", {})
                result = {
                    "game_id": game_id,
                    "beats": self.beats,
                    "accepted": self.accepted,
                }
                self.stop()
                return result

        return {
            "timed_out": True,
            "queue_id": self.queue_id,
            "beats": self.beats,
            "pending_match_id": self.match_id,
            "last_error": self.last_error,
        }


__all__ = ["Matchmaker", "HEARTBEAT_SECONDS", "POLL_SECONDS"]
