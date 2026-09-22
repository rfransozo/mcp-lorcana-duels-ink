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

    def __init__(
        self,
        game: dict | None = None,
        *,
        accept: bool = True,
        logs: list[dict] | None = None,
        silent: bool = False,
        never_ack: bool = False,
        ack_key: str = "success",
        update_delay: float = 0.0,
    ) -> None:
        self.game = game or games.playing()
        self.accept = accept
        # Connect, then say nothing - the real server does this for a game
        # that has finished.
        self.silent = silent
        # Swallow actions without acknowledging them.
        self.never_ack = never_ack
        # Some acknowledgements come back as `ok` rather than `success`.
        self.ack_key = ack_key
        # The real server acknowledges first and pushes the new state a moment
        # later; this reproduces that gap.
        self.update_delay = update_delay
        # Pushed right after init, the way the real server backfills history
        # on connect.
        self.logs = logs or []
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

    async def push(self, message: dict) -> None:
        """Send an unsolicited message, as the server does for presence."""
        for ws in list(self._sockets):
            await ws.send(json.dumps(message))

    async def drop_all(self) -> None:
        """Simulate the connection dropping under us."""
        for ws in list(self._sockets):
            await ws.close()
        self._sockets.clear()

    async def _handle(self, websocket) -> None:
        self.connections += 1
        self._sockets.append(websocket)
        if self.silent:
            try:
                await websocket.wait_closed()
            finally:
                if websocket in self._sockets:
                    self._sockets.remove(websocket)
            return
        await websocket.send(json.dumps({"type": "init", "game": self.game, "buildId": "test-build"}))
        if self.logs:
            await websocket.send(
                json.dumps({"type": "game_log", "logs": self.logs, "fromIndex": 0})
            )
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue
                if message.get("type") != "action":
                    continue
                self.received.append(message)
                if self.never_ack:
                    continue
                if self.accept:
                    await websocket.send(
                        json.dumps({"type": "action_result", self.ack_key: True,
                                    "requestId": message.get("requestId")})
                    )
                    self.game = {**self.game, "stateVersion": self.game.get("stateVersion", 0) + 1}
                    if self.update_delay:
                        await asyncio.sleep(self.update_delay)
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


class TestPresenceAndHeartbeat:
    """Unsolicited pushes: nobody asks for these, they just arrive."""

    async def test_a_disconnect_is_noticed(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.ensure_connected()
        assert conn.opponent_connected is None, "unknown until the server says"
        await server.push({"type": "player_disconnected", "playerNumber": 1})
        await conn.wait_for_update(timeout=2.0)
        assert conn.opponent_connected is False

    async def test_a_reconnect_is_noticed_too(self, ws_setup):
        """An opponent who drops and comes back must not be left marked
        absent - CLAIM_AFK_VICTORY hangs off this."""
        server, conn, _client, _router = ws_setup
        await conn.ensure_connected()
        await server.push({"type": "player_disconnected", "playerNumber": 1})
        await conn.wait_for_update(timeout=2.0)
        await server.push({"type": "player_connected", "playerNumber": 1})
        await conn.wait_for_update(timeout=2.0)
        assert conn.opponent_connected is True

    async def test_a_heartbeat_refreshes_the_build_id(self, ws_setup):
        """The build id is how a deploy mid-game becomes visible."""
        _server, conn, _client, _router = ws_setup
        await conn.ensure_connected()
        assert conn.build_id == "test-build"
        await _server.push({"type": "heartbeat", "timestamp": 1, "buildId": "new-build"})
        await asyncio.sleep(0.1)
        assert conn.build_id == "new-build"

    async def test_a_binary_frame_is_ignored(self, ws_setup):
        """The socket is JSON text; a binary frame must not kill the reader."""
        server, conn, _client, _router = ws_setup
        await conn.ensure_connected()
        for ws in list(server._sockets):
            await ws.send(b"\x00\x01\x02")
        await server.push({"type": "game_update", "game": server.game})
        assert await conn.wait_for_update(timeout=2.0)

    async def test_a_malformed_frame_is_ignored(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.ensure_connected()
        for ws in list(server._sockets):
            await ws.send("not json at all")
        await server.push({"type": "game_update", "game": server.game})
        assert await conn.wait_for_update(timeout=2.0)


class TestAcknowledgements:
    async def test_ok_is_accepted_as_well_as_success(self, router, make_client):
        """Both spellings appear on the wire, and treating `ok` as a failure
        would abort a move that actually landed."""
        async with FakeGameServer(ack_key="ok") as server:
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            conn = GameConnection(make_client(), "g1")
            try:
                result = await conn.send_action({"type": "QUEST"})
                assert result.get("ok") is True
            finally:
                await conn.close()

    async def test_an_unacknowledged_action_says_it_may_have_applied(
        self, router, make_client, monkeypatch
    ):
        """Re-sending a move that silently landed would play it twice."""
        monkeypatch.setattr("src.gamews.ACTION_TIMEOUT_SECONDS", 0.2)
        async with FakeGameServer(never_ack=True) as server:
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            conn = GameConnection(make_client(), "g1")
            try:
                with pytest.raises(DuelsError, match="may have applied"):
                    await conn.send_action({"type": "QUEST"})
            finally:
                await conn.close()

    async def test_sending_on_a_dead_socket_is_actionable(self, ws_setup):
        server, conn, _client, _router = ws_setup
        await conn.ensure_connected()
        await server.drop_all()
        await server.__aexit__()
        with pytest.raises(DuelsError):
            await conn.send_action({"type": "QUEST"})


class TestConnectFailures:
    async def test_a_socket_that_never_sends_state_is_reported(
        self, router, make_client, monkeypatch
    ):
        """Connecting is not the same as being in a game; a finished game
        accepts the socket and then says nothing."""
        monkeypatch.setattr("src.gamews.CONNECT_TIMEOUT_SECONDS", 0.3)
        async with FakeGameServer(silent=True) as server:
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            conn = GameConnection(make_client(), "g1")
            try:
                with pytest.raises(DuelsError, match="never received the initial state"):
                    await conn.ensure_connected()
            finally:
                await conn.close()


class TestRegistrySessions:
    async def test_a_later_session_id_upgrades_an_anonymous_connection(
        self, router, make_client
    ):
        """The first call may not know the sessionId; losing it would make
        every later call on an anonymous game unauthorised."""
        async with FakeGameServer() as server:
            router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": server.url})
            registry = GameRegistry(make_client())
            try:
                conn = await registry.get("g1")
                assert conn.session_id is None
                again = await registry.get("g1", "sess-1")
                assert again is conn
                assert conn.session_id == "sess-1"
            finally:
                await registry.close_all()
