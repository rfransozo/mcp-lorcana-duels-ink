"""Getting into a game takes more than joining the queue, and that is easy to miss.

Joining looks like it worked without the heartbeat: the response carries a
position and an estimated wait, and asking again reports a plausible new one.
No opponent is ever paired. Twenty-one minutes went by that way against
queues that had players in them the whole time.

And once a pairing does arrive it has to be accepted inside about fifteen
seconds. A recording of the real client showed an `opponent_ready` for a
pairing this side never answered - a person had accepted and was left waiting
for a client that was only listening for a game that would never start.

These tests pin both halves, and the traps around them.
"""

import asyncio
import contextlib

import pytest

from src.client import DuelsError
from src.matchmaking import Matchmaker

pytestmark = pytest.mark.anyio


class FakeClient:
    """Records calls and plays back a scripted event stream."""

    def __init__(
        self,
        events: list[dict] | None = None,
        game_id: str | None = None,
        fail_beats: int = 0,
        stream_error: str | None = None,
    ) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.events = events or []
        self.game_id = game_id
        self.fail_beats = fail_beats
        self.stream_error = stream_error

    async def post(self, path: str, body=None):
        self.posts.append((path, body or {}))
        if path.endswith("/heartbeat") and self.fail_beats > 0:
            self.fail_beats -= 1
            raise DuelsError("heartbeat blew up")
        return {"success": True}

    async def get(self, path: str, params=None):
        self.gets.append(path)
        games = [{"id": self.game_id}] if self.game_id else []
        return {"games": games}

    @contextlib.asynccontextmanager
    async def stream_events(self, path: str):
        self.gets.append(path)
        if self.stream_error:
            raise DuelsError(self.stream_error)

        async def gen():
            for event in self.events:
                await asyncio.sleep(0)
                yield event
            # A real stream stays open; hold it so the listener does not
            # finish early and mask a missing event.
            await asyncio.sleep(3600)

        yield gen()

    def posted_to(self, suffix: str) -> list[dict]:
        return [body for path, body in self.posts if path.endswith(suffix)]


def sse(kind: str, **data) -> dict:
    return {"type": kind, "data": data}


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)


class TestHeartbeat:
    async def test_joining_starts_the_heartbeat(self):
        client = FakeClient()
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await _settle()
            assert mm.queued
            assert len(client.posted_to("/heartbeat")) >= 2
        finally:
            await mm.close()

    async def test_stopping_ends_it(self):
        client = FakeClient()
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        await _settle()
        mm.stop()
        await _settle()
        before = len(client.posts)
        await _settle()
        assert len(client.posts) == before
        assert not mm.queued

    async def test_a_failed_beat_does_not_leave_the_queue(self):
        """One dropped request must not silently un-queue us - that is the
        exact failure this class exists to prevent."""
        client = FakeClient(fail_beats=2)
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await _settle(0.1)
            assert mm.queued and mm.beats > 0
        finally:
            await mm.close()


class TestAcceptingThePairing:
    async def test_match_found_is_accepted_at_once(self):
        """The window is about fifteen seconds and a person is already
        waiting, so this cannot wait for the caller to come back."""
        client = FakeClient(events=[sse("match_found", matchId="m-1", expiresAt=1)])
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await _settle(0.1)
            assert client.posted_to("/accept") == [{"matchId": "m-1"}]
            assert mm.accepted
        finally:
            await mm.close()

    async def test_game_starting_yields_the_game(self):
        client = FakeClient(
            events=[
                sse("match_found", matchId="m-1"),
                sse("match_accepted", matchId="m-1"),
                sse("game_starting", gameId="g-9"),
            ]
        )
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=1)
            assert result["game_id"] == "g-9"
            assert result["accepted"] is True
        finally:
            await mm.close()

    async def test_leaving_the_queue_is_part_of_succeeding(self):
        """The site leaves on a match; a stale entry could otherwise pair us
        again mid-game."""
        client = FakeClient(events=[sse("game_starting", gameId="g-9")])
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await mm.wait_for_match(timeout_seconds=1)
            assert client.posted_to("/leave") == [{}]
            assert not mm.queued
        finally:
            await mm.close()

    async def test_a_cancelled_pairing_keeps_us_queued(self):
        client = FakeClient(
            events=[sse("match_found", matchId="m-1"), sse("match_cancelled", matchId="m-1")]
        )
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=0.1)
            assert result["timed_out"] is True
            assert mm.queued, "a cancelled pairing is not a reason to stop waiting"
        finally:
            await mm.close()

    async def test_a_rejected_accept_is_recorded_not_raised(self):
        """Losing the race to the window should leave us queued and say why,
        not kill the wait."""

        class Refusing(FakeClient):
            async def post(self, path, body=None):
                if path.endswith("/accept"):
                    self.posts.append((path, body or {}))
                    raise DuelsError("match expired")
                return await super().post(path, body)

        client = Refusing(events=[sse("match_found", matchId="m-1")])
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=0.1)
            assert result["timed_out"] is True
            assert result["pending_match_id"] == "m-1"
            assert "expired" in (result["last_error"] or "")
            assert not mm.accepted
        finally:
            await mm.close()


class TestWhenTheStreamFails:
    async def test_active_games_still_finds_the_match(self):
        """The events stream was seen returning 503 mid-queue while the queue
        stayed healthy, so the poll has to be able to carry the wait alone."""
        client = FakeClient(game_id="g-3", stream_error="stream refused (503)")
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=1)
            assert result["game_id"] == "g-3"
        finally:
            await mm.close()

    async def test_a_dead_stream_does_not_stop_the_heartbeat(self):
        client = FakeClient(stream_error="stream refused (503)")
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await _settle(0.1)
            assert mm.queued and mm.beats > 0
        finally:
            await mm.close()


class TestWaiting:
    async def test_timing_out_is_not_an_error(self):
        client = FakeClient()
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=0.05)
            assert result["timed_out"] is True
            assert result["queue_id"] == "quick-play"
            assert mm.queued, "timing out must not drop the queue entry"
        finally:
            await mm.close()

    async def test_waiting_keeps_beating(self):
        """Waiting is what makes the match happen, so the beat must continue
        for as long as the caller blocks."""
        client = FakeClient()
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await mm.wait_for_match(timeout_seconds=0.1)
            assert len(client.posted_to("/heartbeat")) >= 3
        finally:
            await mm.close()
