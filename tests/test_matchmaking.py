"""The queue entry has to be kept alive, and that is easy to get wrong.

Joining a Duels.ink queue looks like it worked without the heartbeat: the
response carries a position and an estimated wait, and asking again even
reports a plausible new position. No opponent is ever paired. Twenty-one
minutes were spent queued that way, against a queue that had players in it
the whole time, before watching the site revealed the missing request.

These tests pin the three things that cost that time.
"""

import asyncio

import pytest

from src.client import DuelsError
from src.matchmaking import Matchmaker

pytestmark = pytest.mark.anyio


class FakeClient:
    """Records calls, and can be told when a game appears."""

    def __init__(self, game_id: str | None = None, fail_beats: int = 0) -> None:
        self.posts: list[str] = []
        self.gets: list[str] = []
        self.game_id = game_id
        self.fail_beats = fail_beats

    async def post(self, path: str, body=None):
        self.posts.append(path)
        if path.endswith("/heartbeat") and self.fail_beats > 0:
            self.fail_beats -= 1
            raise DuelsError("heartbeat blew up")
        return {"success": True}

    async def get(self, path: str, params=None):
        self.gets.append(path)
        games = [{"id": self.game_id}] if self.game_id else []
        return {"games": games}


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


class TestHeartbeat:
    async def test_joining_starts_the_heartbeat(self, monkeypatch):
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        client = FakeClient()
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await _settle()
            assert mm.queued
            assert client.posts.count("/api/matchmaking/heartbeat") >= 2
        finally:
            await mm.close()

    async def test_stopping_ends_it(self, monkeypatch):
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
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

    async def test_a_failed_beat_does_not_leave_the_queue(self, monkeypatch):
        """One dropped request must not silently un-queue us - that is the
        exact failure this class exists to prevent."""
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        client = FakeClient(fail_beats=2)
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await _settle(0.1)
            assert mm.queued
            assert mm.beats > 0, "should have recovered and kept beating"
        finally:
            await mm.close()


class TestWaitingForAMatch:
    async def test_returns_the_game_and_leaves_the_queue(self, monkeypatch):
        """The site leaves the queue itself on a match; a stale entry could
        otherwise pair us again mid-game."""
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)
        client = FakeClient(game_id="game-7")
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=1)
            assert result["game_id"] == "game-7"
            assert "/api/matchmaking/leave" in client.posts
            assert not mm.queued
        finally:
            await mm.close()

    async def test_timing_out_is_not_an_error(self, monkeypatch):
        """Still queued is a normal answer - the caller just waits again."""
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)
        client = FakeClient(game_id=None)
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            result = await mm.wait_for_match(timeout_seconds=0.05)
            assert result["timed_out"] is True
            assert result["queue_id"] == "quick-play"
            assert mm.queued, "timing out must not drop the queue entry"
        finally:
            await mm.close()

    async def test_waiting_keeps_beating(self, monkeypatch):
        """Waiting is what makes the match happen, so the beat must continue
        for as long as the caller blocks."""
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)
        client = FakeClient(game_id=None)
        mm = Matchmaker(client)
        mm.start("quick-play", "deck-1")
        try:
            await mm.wait_for_match(timeout_seconds=0.1)
            assert client.posts.count("/api/matchmaking/heartbeat") >= 3
        finally:
            await mm.close()
