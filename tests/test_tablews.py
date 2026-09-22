"""Tests for the lobby WebSocket layer.

Like the game socket tests, these run against a real in-process server that
speaks the table protocol, so connecting, resyncing and reconnecting are
exercised rather than mocked.

What the lobby socket is *for* is worth restating, because it is not obvious
from the code: `connected` on a seat means "this player holds a socket". With
no socket our seat read as empty to everybody else at the table, ready and
decked and apparently abandoned.
"""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from src.client import DuelsError
from src.tablews import TableConnection, TableRegistry

pytestmark = pytest.mark.anyio

TABLE_ID = "01a0c67a-16a6-7907-82f7-2c45d5bcb4aa"


def a_view(**overrides) -> dict:
    view = {
        "id": TABLE_ID,
        "status": "assembling",
        "version": 1,
        "mySeatIndex": 0,
        "isHost": True,
        "config": {"maxSeats": 4, "gameFormat": "Coconut", "timerPreset": "none"},
        "seats": [{"index": 0, "username": "Fransozo", "connected": True, "ready": False}],
        "spectators": [],
        "log": [],
    }
    view.update(overrides)
    return view


class FakeTableServer:
    """Minimal stand-in for the Duels.ink lobby socket.

    Sends `table_init` on connect, answers `ping` with `pong`, and pushes
    `table_update` when told to. It deliberately ignores `action` frames: the
    real server accepts them and does nothing, which is exactly the trap this
    module exists to document.
    """

    def __init__(self, view: dict | None = None, *, silent: bool = False) -> None:
        self.view = view or a_view()
        self.silent = silent
        self.connections = 0
        self.received: list[dict] = []
        self._server = None
        self._sockets: list = []

    async def __aenter__(self) -> "FakeTableServer":
        self._server = await serve(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        sock = next(iter(self._server.sockets))
        return f"ws://127.0.0.1:{sock.getsockname()[1]}"

    async def push_update(self, view: dict) -> None:
        self.view = view
        for ws in list(self._sockets):
            await ws.send(json.dumps({"type": "table_update", "view": view}))

    async def drop_all(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        self._sockets.clear()

    async def _handle(self, websocket) -> None:
        self.connections += 1
        self._sockets.append(websocket)
        try:
            if self.silent:
                await websocket.wait_closed()
                return
            await websocket.send(json.dumps({"type": "table_init", "view": self.view}))
            async for raw in websocket:
                message = json.loads(raw)
                self.received.append(message)
                if message.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
        except Exception:
            pass
        finally:
            if websocket in self._sockets:
                self._sockets.remove(websocket)


@pytest.fixture
async def lobby(router, make_client):
    """A TableConnection pointed at a fake server, via the real token flow."""
    async with FakeTableServer() as server:
        client = make_client()
        # A table's token comes back with no host at all; the client derives it.
        router.json_on(f"/api/table/{TABLE_ID}/ws-token", {"token": "tok", "wsUrl": server.url})
        conn = TableConnection(client, TABLE_ID)
        yield server, conn, client, router
        await conn.close()
        await client.aclose()


class TestConnecting:
    async def test_connect_receives_the_view(self, lobby):
        _server, conn, _client, _router = lobby
        await conn.connect()
        assert conn.connected
        assert (await conn.refresh())["id"] == TABLE_ID

    async def test_connect_is_idempotent(self, lobby):
        server, conn, _client, _router = lobby
        await conn.connect()
        await conn.connect()
        assert server.connections == 1

    async def test_a_silent_server_is_not_a_connection(self, router, make_client):
        """Connected but never told anything is worse than refused: the seat
        looks occupied while we know nothing about the table."""
        async with FakeTableServer(silent=True) as server:
            client = make_client()
            router.json_on(
                f"/api/table/{TABLE_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            conn = TableConnection(client, TABLE_ID)
            import src.tablews as tablews

            original = tablews.CONNECT_TIMEOUT_SECONDS
            tablews.CONNECT_TIMEOUT_SECONDS = 0.4
            try:
                with pytest.raises(DuelsError, match="never received its state"):
                    await conn.connect()
            finally:
                tablews.CONNECT_TIMEOUT_SECONDS = original
                await conn.close()
                await client.aclose()

    async def test_an_unreachable_table_says_so(self, router, make_client):
        client = make_client()
        router.json_on(
            f"/api/table/{TABLE_ID}/ws-token", {"token": "t", "wsUrl": "ws://127.0.0.1:1"}
        )
        conn = TableConnection(client, TABLE_ID)
        with pytest.raises(DuelsError, match="Could not open"):
            await conn.connect()
        await client.aclose()


class TestStayingInSync:
    async def test_an_update_replaces_the_view(self, lobby):
        server, conn, _client, _router = lobby
        await conn.connect()
        await server.push_update(a_view(version=2, status="ready"))
        assert await conn.wait_for_update(timeout=2)
        assert (await conn.refresh())["version"] == 2

    async def test_wait_returns_none_when_nothing_happens(self, lobby):
        _server, conn, _client, _router = lobby
        await conn.connect()
        assert await conn.wait_for_update(timeout=0.3) is None

    async def test_a_dropped_socket_reconnects_and_resyncs(self, lobby):
        """`table_init` carries the whole view, so nothing has to be replayed."""
        server, conn, _client, _router = lobby
        await conn.connect()
        await server.push_update(a_view(version=7))
        await conn.wait_for_update(timeout=2)
        await server.drop_all()
        await asyncio.sleep(0.2)
        assert (await conn.refresh())["version"] == 7
        assert server.connections == 2

    async def test_a_ping_keeps_the_seat_warm(self, lobby):
        server, conn, _client, _router = lobby
        import src.tablews as tablews

        original = tablews.PING_INTERVAL_SECONDS
        tablews.PING_INTERVAL_SECONDS = 0.1
        try:
            await conn.connect()
            await asyncio.sleep(0.35)
        finally:
            tablews.PING_INTERVAL_SECONDS = original
        assert any(m.get("type") == "ping" for m in server.received)

    async def test_rubbish_frames_are_ignored(self, lobby):
        server, conn, _client, _router = lobby
        await conn.connect()
        for ws in list(server._sockets):
            await ws.send("not json at all")
            await ws.send(json.dumps({"type": "table_update", "view": "not a dict"}))
            await ws.send(json.dumps({"type": "something_new"}))
        await asyncio.sleep(0.2)
        assert (await conn.refresh())["id"] == TABLE_ID

    async def test_closing_twice_is_harmless(self, lobby):
        _server, conn, _client, _router = lobby
        await conn.connect()
        await conn.close()
        await conn.close()
        assert not conn.connected


class TestTheRegistry:
    async def test_one_connection_per_table(self, lobby):
        server, _conn, client, _router = lobby
        registry = TableRegistry(client)
        first = await registry.get(TABLE_ID)
        second = await registry.get(TABLE_ID)
        assert first is second
        assert server.connections == 1
        await registry.close_all()

    async def test_attend_returns_the_view(self, lobby):
        _server, _conn, client, _router = lobby
        registry = TableRegistry(client)
        view = await registry.attend(TABLE_ID)
        assert view and view["id"] == TABLE_ID
        await registry.close_all()

    async def test_attend_waits_for_the_presence_broadcast(self, lobby):
        """`table_init` is written before the server announces that we arrived.

        Rendering it verbatim made a freshly created table report itself
        abandoned on the very next line, because our own seat still read as
        absent in the state we had just been handed.
        """
        server, _conn, client, _router = lobby
        registry = TableRegistry(client)

        async def announce_later():
            await asyncio.sleep(0.1)
            await server.push_update(
                a_view(seats=[{"index": 0, "username": "Fransozo",
                               "connected": True, "ready": True}])
            )

        task = asyncio.create_task(announce_later())
        view = await registry.attend(TABLE_ID)
        await task
        assert view["seats"][0]["ready"] is True
        await registry.close_all()

    async def test_attend_can_skip_the_wait(self, lobby):
        """Nothing is pending, so it should not sit through the grace period."""
        _server, _conn, client, _router = lobby
        registry = TableRegistry(client)
        view = await registry.attend(TABLE_ID, fresh=False)
        assert view and view["id"] == TABLE_ID
        await registry.close_all()

    async def test_attend_never_fails_the_caller(self, router, make_client):
        """Presence is a bonus on top of a table call.

        Turning a successful set_deck into an error because a socket would not
        open trades a real result for a cosmetic one.
        """
        client = make_client()
        router.json_on(
            f"/api/table/{TABLE_ID}/ws-token", {"token": "t", "wsUrl": "ws://127.0.0.1:1"}
        )
        registry = TableRegistry(client)
        assert await registry.attend(TABLE_ID) is None
        await registry.close_all()
        await client.aclose()

    async def test_dropping_closes_and_forgets(self, lobby):
        _server, _conn, client, _router = lobby
        registry = TableRegistry(client)
        await registry.get(TABLE_ID)
        assert registry.active_ids() == [TABLE_ID]
        await registry.drop(TABLE_ID)
        assert registry.active_ids() == []
        await registry.close_all()

    async def test_dropping_an_unknown_table_is_harmless(self, lobby):
        _server, _conn, client, _router = lobby
        registry = TableRegistry(client)
        await registry.drop("never-seen")
        await registry.close_all()

    async def test_close_all_empties_the_registry(self, lobby):
        _server, _conn, client, _router = lobby
        registry = TableRegistry(client)
        await registry.get(TABLE_ID)
        await registry.close_all()
        assert registry.active_ids() == []
