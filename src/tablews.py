"""Live lobby connection over the Duels.ink table WebSocket.

Protocol, as observed on the wire:

Client to server
    {"type": "ping"}

Server to client
    {"type": "table_init",   "view": {...}}
    {"type": "table_update", "view": {...}}
    {"type": "pong"}

Three things make this different from the game socket, all found the hard way
against a real table:

* **The token endpoint names no host.** Games are sharded and their token
  carries a `wsUrl`; `/api/table/{id}/ws-token` answers with a bare JWT. Code
  that insisted on `wsUrl` could never open a lobby socket at all.
* **Actions do not travel on it.** Sending `{"type": "action", ...}` is
  accepted and changes nothing - every lobby action is a POST to `/action`.
  So this connection only ever reads.
* **It is the seat's presence.** `connected` on a seat means "has a socket
  open". Without one we sat at a table looking permanently absent, which is
  grounds for a host to kick the chair before the game starts. That is the
  whole reason this module exists: the view it receives is a convenience, the
  connection itself is the point.
"""

import asyncio
import json
import logging
from typing import Any, Optional

import websockets

from .client import DuelsClient, DuelsError

log = logging.getLogger("duels_mcp.tablews")

PING_INTERVAL_SECONDS = 25.0
CONNECT_TIMEOUT_SECONDS = 20.0
# How long to let the server's presence broadcast catch up before rendering.
PRESENCE_GRACE_SECONDS = 2.0


class TableConnection:
    """One live WebSocket attached to one lobby."""

    def __init__(self, client: DuelsClient, table_id: str) -> None:
        self._client = client
        self.table_id = table_id

        self._ws: Optional[Any] = None
        self._reader: Optional[asyncio.Task] = None
        self._pinger: Optional[asyncio.Task] = None

        self.view: Optional[dict] = None
        self._updated = asyncio.Event()
        self._ready = asyncio.Event()
        self._closed = False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closed

    async def connect(self) -> None:
        """Open the socket and block until the first `table_init` arrives."""
        if self.connected and self._ready.is_set():
            return

        url = await self._client.ws_token("table", self.table_id)
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url, max_size=4 * 1024 * 1024),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise DuelsError(
                f"Timed out connecting to the Duels.ink lobby socket for {self.table_id}."
            ) from exc
        except Exception as exc:
            raise DuelsError(
                f"Could not open the Duels.ink lobby socket for table {self.table_id} "
                f"({type(exc).__name__}). The table may have been cancelled, or the "
                "token expired."
            ) from exc

        self._closed = False
        self._ready.clear()
        self._reader = asyncio.create_task(self._read_loop())
        self._pinger = asyncio.create_task(self._ping_loop())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise DuelsError(
                f"Connected to table {self.table_id} but never received its state. "
                "The table may no longer exist."
            ) from exc

    async def ensure_connected(self) -> None:
        """Reconnect when the socket has dropped; `table_init` resyncs in full."""
        if not self.connected or not self._ready.is_set():
            await self.connect()

    async def close(self) -> None:
        self._closed = True
        for task in (self._pinger, self._reader):
            if task and not task.done():
                task.cancel()
        self._pinger = self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._ready.clear()

    # -----------------------------------------------------------------
    # Background loops
    # -----------------------------------------------------------------
    async def _ping_loop(self) -> None:
        try:
            while not self._closed and self._ws is not None:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                if self._ws is None or self._closed:
                    return
                try:
                    await self._ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._handle(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.info("Lobby socket %s closed: %s", self.table_id, type(exc).__name__)
        finally:
            self._ws = None
            self._ready.clear()

    def _handle(self, msg: dict) -> None:
        # `table_init` and `table_update` carry the same complete view, so
        # neither needs to be merged into the other - the last one wins.
        if msg.get("type") in ("table_init", "table_update"):
            view = msg.get("view")
            if isinstance(view, dict):
                self.view = view
                self._ready.set()
                self._wake()

    def _wake(self) -> None:
        self._updated.set()
        self._updated = asyncio.Event()

    # -----------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------
    async def wait_for_update(self, timeout: float) -> Optional[dict]:
        """Block until the lobby changes, or give up and return None.

        Used to wait for players to sit down without polling the REST view,
        which is both slower and, because it does not hold a socket, invisible
        to everyone else at the table.
        """
        await self.ensure_connected()
        waiter = self._updated
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self.view

    async def refresh(self) -> dict:
        """The current view, connecting first if needed."""
        await self.ensure_connected()
        return self.view or {}


class TableRegistry:
    """Keeps one TableConnection per lobby for the server's lifetime.

    A connection is held open rather than opened per call because it is what
    marks the seat as occupied: churn would make the seat blink in and out for
    everyone else.
    """

    def __init__(self, client: DuelsClient) -> None:
        self._client = client
        self._tables: dict[str, TableConnection] = {}
        self._lock = asyncio.Lock()

    async def get(self, table_id: str) -> TableConnection:
        async with self._lock:
            conn = self._tables.get(table_id)
            if conn is None:
                conn = TableConnection(self._client, table_id)
                self._tables[table_id] = conn
        await conn.ensure_connected()
        return conn

    async def attend(self, table_id: str, *, fresh: bool = True) -> Optional[dict]:
        """Hold a socket open for a table, without failing the caller if it cannot.

        Presence is a nice-to-have on top of every table call: a lobby that
        refuses a socket is still perfectly usable over REST, and turning a
        successful `set_deck` into an error because the socket dropped would
        trade a real result for a cosmetic one.
        """
        try:
            conn = await self.get(table_id)
        except DuelsError as exc:
            log.info("Not attending table %s: %s", table_id, exc)
            return None

        if fresh:
            # `table_init` is written before the server has broadcast that we
            # arrived, so it still shows our own seat as absent. Rendering it
            # made a table report itself abandoned one line after we created
            # it. One bounded wait picks up the presence broadcast; if it does
            # not come, the init view is still perfectly usable.
            await conn.wait_for_update(PRESENCE_GRACE_SECONDS)
        return conn.view

    async def drop(self, table_id: str) -> None:
        """Close and forget one lobby (after leaving, cancelling or starting)."""
        async with self._lock:
            conn = self._tables.pop(table_id, None)
        if conn:
            await conn.close()

    def active_ids(self) -> list[str]:
        return list(self._tables)

    async def close_all(self) -> None:
        async with self._lock:
            conns = list(self._tables.values())
            self._tables.clear()
        for conn in conns:
            await conn.close()
