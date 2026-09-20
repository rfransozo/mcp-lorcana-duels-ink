"""Tests for the live game WebSocket layer.

These run against a real WebSocket server started in-process that speaks the
Duels.ink protocol, so connection handling, reconnection and action matching
are exercised for real rather than mocked away.
"""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from src.client import DuelsError
from src.gamews import GameConnection, GameRegistry
from tests.fixtures import games


class FakeGameServer:
    """Minimal stand-in for the Duels.ink game socket.

    Sends `init` on connect, then answers actions with `action_result` followed
    by a `game_update`, which is the real exchange.
    """

    def __init__(self, game: dict | None = None, *, accept: bool = True) -> None:
        self.game = game or games.playing()
        self.accept = accept
        self.received: list[dict] = []
        self.connections = 0
        self._server = None
        self._sockets: list = []

    async def __aenter__(self) -> "FakeGameServer":
        self._server = await serve(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        sock = next(iter(self._server.sockets))
        return f"ws://127.0.0.1:{sock.getsockname()[1]}"

    async def drop_all(self) -> None:
        """Simulate the connection dropping under us."""
        for ws in list(self._sockets):
            await ws.close()
        self._sockets.clear()

    async def _handle(self, websocket) -> None:
        self.connections += 1
        self._sockets.append(websocket)
        await websocket.send(json.dumps({"type": "init", "game": self.game, "buildId": "test-build"}))
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue
                if message.get("type") != "action":
                    continue
                self.received.append(message)
                if self.accept:
                    await websocket.send(
                        json.dumps({"type": "action_result", "success": True,
                                    "requestId": message.get("requestId")})
                    )
                    self.game = {**self.game, "stateVersion": self.game.get("stateVersion", 0) + 1}
                    await websocket.send(json.dumps({"type": "game_update", "game": self.game}))
                else:
                    await websocket.send(
                        json.dumps({"type": "action_result", "success": False,
                                    "error": "Not enough ink"})
                    )
        except Exception:
            pass
        finally:
            if websocket in self._sockets:
                self._sockets.remove(websocket)


@pytest.fixture
async def ws_setup(router, make_client):
    """A GameConnection pointed at a fake server, via the real ws-token flow."""
    async with FakeGameServer() as server:
        client = make_client()
        router.json_on(
            "/api/game/g1/ws-token", {"token": "tok", "wsUrl": server.url}
        )
        conn = GameConnection(client, "g1")
        yield server, conn, client, router
        await conn.close()
        await client.aclose()


class TestConnect:
    async def test_connect_receives_the_initial_state(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.connect()
        assert conn.connected
        assert conn.build_id == "test-build"
        assert (await conn.refresh())["id"] == games.GAME_ID

    async def test_the_token_url_is_used(self, ws_setup):
        _server, conn, _client, router = ws_setup
        await conn.connect()
        assert router.count("/api/game/g1/ws-token") == 1

    async def test_anonymous_session_id_is_forwarded(self, router, make_client):
        async with FakeGameServer() as server:
            client = make_client()
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            conn = GameConnection(client, "g1", session_id="sess-9")
            await conn.connect()
            assert router.calls[-1].url.params["sessionId"] == "sess-9"
            await conn.close()
            await client.aclose()

    async def test_connect_is_idempotent(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.connect()
        await conn.connect()
        assert server.connections == 1

    async def test_unreachable_server_raises_an_actionable_error(self, router, make_client):
        client = make_client()
        router.json_on(
            "/api/game/g1/ws-token", {"token": "t", "wsUrl": "ws://127.0.0.1:1"}
        )
        conn = GameConnection(client, "g1")
        with pytest.raises(DuelsError, match="game may have ended|Could not open"):
            await conn.connect()
        await client.aclose()


class TestSendAction:
    async def test_action_is_sent_in_the_protocol_envelope(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.connect()
        await conn.send_action({"type": "ADD_TO_INK", "cardInstanceId": "c1"})

        sent = server.received[-1]
        assert sent["type"] == "action"
        assert sent["action"] == {"type": "ADD_TO_INK", "cardInstanceId": "c1"}
        assert sent["requestId"], "every action must carry a requestId"

    async def test_each_action_gets_a_fresh_request_id(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.connect()
        await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})
        await conn.send_action({"type": "QUEST", "cardInstanceId": "c2"})
        assert server.received[0]["requestId"] != server.received[1]["requestId"]

    async def test_state_is_refreshed_after_the_action(self, ws_setup):
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        before = (await conn.refresh())["stateVersion"]
        await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})
        assert (await conn.refresh())["stateVersion"] > before

    async def test_rejection_points_at_the_legal_moves_tool(self, router, make_client):
        async with FakeGameServer(accept=False) as server:
            client = make_client()
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            conn = GameConnection(client, "g1")
            await conn.connect()
            with pytest.raises(DuelsError) as exc:
                await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})
            message = str(exc.value)
            assert "Not enough ink" in message
            assert "duels_get_legal_moves" in message
            await conn.close()
            await client.aclose()

    async def test_action_does_not_pay_the_update_timeout(self, ws_setup):
        """Regression: _wake() used set-then-clear, so the game_update that
        arrives right after the ack was missed and every action sat out the
        full 3s wait - about 90 seconds across a normal game."""
        _server, conn, _client, _router = ws_setup
        await conn.connect()

        started = asyncio.get_running_loop().time()
        await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 1.0, f"send_action took {elapsed:.2f}s; the state push was missed"

    async def test_a_wake_up_is_never_lost(self, ws_setup):
        """The event a waiter is parked on must still be set after _wake.

        `send_action` is protected by the revision counter, but callers that
        wait on a predicate - duels_wait_for_my_turn above all - only have the
        event. Setting and immediately clearing it drops the notification for
        anyone who had already captured it, and they sit out the full timeout.
        """
        _server, conn, _client, _router = ws_setup
        await conn.connect()

        parked_on = conn._updated          # what a waiter would be awaiting
        conn._handle({"type": "game_update", "game": games.playing()})

        assert parked_on.is_set(), "the parked waiter was never notified"
        assert conn._updated is not parked_on, "a fresh event must replace the old one"

    async def test_predicate_waiter_wakes_without_paying_the_timeout(self, ws_setup):
        """End-to-end version of the same invariant, through the public API."""
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        target = (await conn.refresh())["stateVersion"] + 5

        async def push_later():
            await asyncio.sleep(0.05)
            conn._handle(
                {"type": "game_update", "game": {**games.playing(), "stateVersion": target}}
            )

        task = asyncio.create_task(push_later())
        started = asyncio.get_running_loop().time()
        got = await conn.wait_for_update(
            timeout=5.0, predicate=lambda g: g and g.get("stateVersion") == target
        )
        elapsed = asyncio.get_running_loop().time() - started
        await task

        assert got is True
        assert elapsed < 1.0, f"waiter took {elapsed:.2f}s; the notification was lost"

    async def test_revision_advances_on_each_push(self, ws_setup):
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        before = conn.revision
        await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})
        assert conn.revision > before

    async def test_wait_with_since_returns_immediately_when_already_moved(self, ws_setup):
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        stale = conn.revision - 1
        assert await conn.wait_for_update(timeout=5.0, since=stale) is True

    async def test_actions_are_serialised(self, ws_setup):
        """One action in flight at a time is what lets action_result be matched
        without relying on the server echoing our requestId."""
        server, conn, _client, _router = ws_setup
        await conn.connect()
        await asyncio.gather(
            conn.send_action({"type": "QUEST", "cardInstanceId": "a"}),
            conn.send_action({"type": "QUEST", "cardInstanceId": "b"}),
            conn.send_action({"type": "QUEST", "cardInstanceId": "c"}),
        )
        assert len(server.received) == 3


class TestReconnection:
    async def test_dropped_connection_is_restored_with_a_full_resync(self, ws_setup):
        """`init` always carries the complete state, which is what makes
        reconnection a resync rather than a replay."""
        server, conn, _client, _router = ws_setup
        await conn.connect()
        assert server.connections == 1

        await server.drop_all()
        await asyncio.sleep(0.2)

        state = await conn.refresh()
        assert state["id"] == games.GAME_ID
        assert server.connections == 2

    async def test_actions_work_again_after_a_drop(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.connect()
        await server.drop_all()
        await asyncio.sleep(0.2)
        await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})
        assert server.received


class TestWaitForUpdate:
    async def test_returns_true_when_the_predicate_already_holds(self, ws_setup):
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        assert await conn.wait_for_update(timeout=0.1, predicate=lambda g: g is not None)

    async def test_times_out_without_raising(self, ws_setup):
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        assert await conn.wait_for_update(timeout=0.2, predicate=lambda g: False) is False

    async def test_wakes_on_a_state_push(self, ws_setup):
        _server, conn, _client, _router = ws_setup
        await conn.connect()
        target = (await conn.refresh())["stateVersion"] + 1

        async def act():
            await asyncio.sleep(0.05)
            await conn.send_action({"type": "QUEST", "cardInstanceId": "c1"})

        task = asyncio.create_task(act())
        got = await conn.wait_for_update(
            timeout=3.0, predicate=lambda g: g and g.get("stateVersion", 0) >= target
        )
        await task
        assert got is True


class TestRegistry:
    async def test_connection_is_reused_across_calls(self, router, make_client):
        async with FakeGameServer() as server:
            client = make_client()
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            registry = GameRegistry(client)
            a = await registry.get("g1")
            b = await registry.get("g1")
            assert a is b
            assert server.connections == 1
            await registry.close_all()
            await client.aclose()

    async def test_drop_closes_and_forgets(self, router, make_client):
        async with FakeGameServer() as server:
            client = make_client()
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            registry = GameRegistry(client)
            await registry.get("g1")
            assert registry.active_ids() == ["g1"]
            await registry.drop("g1")
            assert registry.active_ids() == []
            await client.aclose()

    async def test_anonymous_session_ids_are_remembered(self, make_client):
        client = make_client()
        registry = GameRegistry(client)
        registry.remember_session("g1", "sess-1")
        assert registry.session_for("g1") == "sess-1"
        assert registry.session_for("unknown") is None
        await client.aclose()

    async def test_remembering_none_is_a_no_op(self, make_client):
        """Signed-in games get sessionId: null; that must not be stored."""
        client = make_client()
        registry = GameRegistry(client)
        registry.remember_session("g1", None)
        assert registry.session_for("g1") is None
        await client.aclose()

    async def test_close_all_releases_every_connection(self, router, make_client):
        async with FakeGameServer() as server:
            client = make_client()
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            router.json_on("/api/game/g2/ws-token", {"token": "t", "wsUrl": server.url})
            registry = GameRegistry(client)
            await registry.get("g1")
            await registry.get("g2")
            await registry.close_all()
            assert registry.active_ids() == []
            await client.aclose()
