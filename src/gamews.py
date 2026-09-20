"""Live game connection over the Duels.ink game WebSocket.

Protocol, as observed on the wire:

Client to server
    {"type": "action", "action": {...}, "requestId": "<uuid>"}
    {"type": "ping"}

Server to client
    {"type": "init",          "game": {...}, "buildId": "..."}
    {"type": "game_update",   "game": {...}}
    {"type": "game_log",      "logs": [...], "fromIndex": N, "isGameFinished": bool}
    {"type": "action_result", "success": bool, ...}
    {"type": "heartbeat",     "timestamp": N, "stateVersion": N, "buildId": "..."}
    {"type": "pong"}
    {"type": "player_disconnected", "playerNumber": N}

Two design points worth knowing:

* `init` always carries the **complete** game state, so a dropped connection is
  recovered by reconnecting - no replay or delta bookkeeping is needed.
* The connection is kept alive for the duration of a game rather than opened
  per call. The server broadcasts `player_disconnected` and the rule set has a
  `CLAIM_AFK_VICTORY` action, so repeated connect/disconnect churn could look
  like an absent player.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Optional

import websockets

from .client import DuelsClient, DuelsError

log = logging.getLogger("duels_mcp.gamews")

PING_INTERVAL_SECONDS = 25.0
ACTION_TIMEOUT_SECONDS = 20.0
CONNECT_TIMEOUT_SECONDS = 20.0
IDLE_CLOSE_SECONDS = 15 * 60


class GameConnection:
    """One live WebSocket attached to one game."""

    def __init__(
        self,
        client: DuelsClient,
        game_id: str,
        session_id: Optional[str] = None,
    ) -> None:
        self._client = client
        self.game_id = game_id
        # Anonymous games carry no cookie; their only credential is the
        # sessionId handed back by create-bot-game.
        self.session_id = session_id

        self._ws: Optional[Any] = None
        self._reader: Optional[asyncio.Task] = None
        self._pinger: Optional[asyncio.Task] = None

        self.game: Optional[dict] = None
        self.build_id: Optional[str] = None
        self.logs: list[dict] = []
        self.opponent_connected: Optional[bool] = None

        self._revision = 0
        self._updated = asyncio.Event()
        self._ready = asyncio.Event()
        self._action_lock = asyncio.Lock()
        self._pending: Optional[asyncio.Future] = None
        self._closed = False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closed

    async def connect(self) -> None:
        """Open the socket and block until the first `init` has arrived."""
        if self.connected and self._ready.is_set():
            return

        url = await self._client.ws_token("game", self.game_id, self.session_id)
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url, max_size=8 * 1024 * 1024),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise DuelsError(
                f"Timed out connecting to the Duels.ink game socket for {self.game_id}."
            ) from exc
        except Exception as exc:
            raise DuelsError(
                f"Could not open the Duels.ink game socket for {self.game_id} "
                f"({type(exc).__name__}). The game may have ended, or the token expired."
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
                f"Connected to game {self.game_id} but never received the initial state. "
                "The game may no longer be active."
            ) from exc

    async def ensure_connected(self) -> None:
        """Reconnect (and full-resync from `init`) when the socket has dropped."""
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
            log.info("Game socket %s closed: %s", self.game_id, type(exc).__name__)
        finally:
            self._ws = None
            self._ready.clear()
            if self._pending and not self._pending.done():
                self._pending.set_exception(
                    DuelsError(
                        "The Duels.ink game socket closed while an action was in flight. "
                        "Re-read the state with duels_get_game_state before retrying."
                    )
                )

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")

        if kind in ("init", "game_update"):
            game = msg.get("game")
            if isinstance(game, dict):
                self.game = game
            if kind == "init":
                self.build_id = msg.get("buildId") or self.build_id
                self.logs = []
                self._ready.set()
            self._wake()

        elif kind == "game_log":
            entries = msg.get("logs") or []
            if isinstance(entries, list):
                known = {e.get("id") for e in self.logs}
                self.logs.extend(e for e in entries if e.get("id") not in known)
            self._wake()

        elif kind == "action_result":
            if self._pending and not self._pending.done():
                self._pending.set_result(msg)

        elif kind == "heartbeat":
            self.build_id = msg.get("buildId") or self.build_id

        elif kind == "player_disconnected":
            self.opponent_connected = False
            self._wake()

        elif kind == "player_connected":
            self.opponent_connected = True
            self._wake()

    @property
    def revision(self) -> int:
        """Counter bumped on every state push.

        Callers snapshot this before doing something that will cause an update,
        so a server that answers faster than they can start waiting is not
        missed.
        """
        return self._revision

    def _wake(self) -> None:
        # Swap in a fresh event rather than set-then-clear: clearing immediately
        # loses the wake-up for anyone who has not started waiting yet, which
        # made every action pay the full timeout.
        self._revision += 1
        previous, self._updated = self._updated, asyncio.Event()
        previous.set()

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------
    async def send_action(self, action: dict) -> dict:
        """Send one game action and wait for the server's verdict.

        Actions are serialised with a lock so exactly one is ever in flight,
        which is what lets `action_result` be matched without relying on the
        server echoing back our requestId.

        Args:
            action: The action payload, e.g. {"type": "ADD_TO_INK",
                "cardInstanceId": "..."}.

        Returns:
            The raw `action_result` message.

        Raises:
            DuelsError: On rejection, timeout, or a dropped socket.
        """
        async with self._action_lock:
            await self.ensure_connected()

            # Snapshot before sending so the follow-up state push cannot be
            # missed, however fast the server answers.
            revision = self._revision
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            payload = {
                "type": "action",
                "action": action,
                "requestId": str(uuid.uuid4()),
            }

            try:
                await self._ws.send(json.dumps(payload))  # type: ignore[union-attr]
            except Exception as exc:
                self._pending = None
                raise DuelsError(
                    f"Failed to send {action.get('type')} to game {self.game_id} "
                    f"({type(exc).__name__}). The connection dropped - retry."
                ) from exc

            try:
                result = await asyncio.wait_for(self._pending, timeout=ACTION_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise DuelsError(
                    f"Duels.ink did not acknowledge {action.get('type')} within "
                    f"{ACTION_TIMEOUT_SECONDS:.0f}s. Re-read the state with "
                    "duels_get_game_state before retrying - it may have applied."
                ) from exc
            finally:
                self._pending = None

        success = result.get("success")
        if success is None:
            success = result.get("ok", True)
        if not success:
            reason = result.get("error") or result.get("message") or "no reason given"
            raise DuelsError(
                f"Duels.ink rejected {action.get('type')}: {reason}. "
                "Call duels_get_legal_moves to see what is actually playable right now."
            )

        # The state push follows the ack; give it a moment to land so callers
        # observe the post-action state.
        await self.wait_for_update(timeout=3.0, since=revision)
        return result

    async def wait_for_update(
        self,
        timeout: float = 30.0,
        predicate: Optional[Callable[[Optional[dict]], bool]] = None,
        since: Optional[int] = None,
    ) -> bool:
        """Wait for the next state push, optionally until a condition holds.

        Args:
            timeout: Seconds to wait before giving up.
            predicate: Return once this holds for the current state. Without
                one, any state push satisfies the wait.
            since: A revision snapshot taken before the triggering call. If the
                state already moved past it, return immediately instead of
                waiting for an update that has already happened.

        Returns:
            True when the wait was satisfied, False on timeout.
        """
        if predicate is not None and predicate(self.game):
            return True
        if predicate is None and since is not None and self._revision != since:
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            # Capture the current event before awaiting; _wake swaps it out.
            waiter = self._updated
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(waiter.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if predicate is None or predicate(self.game):
                return True

    async def refresh(self) -> dict:
        """Return the current game state, reconnecting if necessary."""
        await self.ensure_connected()
        if self.game is None:
            raise DuelsError(
                f"No state available for game {self.game_id}. "
                "It may have finished or never started."
            )
        return self.game


class GameRegistry:
    """Keeps one GameConnection per active game for the server's lifetime."""

    def __init__(self, client: DuelsClient) -> None:
        self._client = client
        self._games: dict[str, GameConnection] = {}
        self._anon_sessions: dict[str, str] = {}
        self._decks: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        game_id: str,
        session_id: Optional[str] = None,
    ) -> GameConnection:
        """Return the connection for a game, opening it on first use.

        `session_id` only matters for anonymous games; it is remembered so
        later calls do not have to repeat it.
        """
        async with self._lock:
            conn = self._games.get(game_id)
            if conn is None:
                conn = GameConnection(self._client, game_id, session_id)
                self._games[game_id] = conn
            elif session_id and not conn.session_id:
                conn.session_id = session_id
        await conn.ensure_connected()
        return conn

    def remember_session(self, game_id: str, session_id: Optional[str]) -> None:
        """Record the anonymous session id for a game created this run."""
        if session_id:
            self._anon_sessions[game_id] = session_id

    def session_for(self, game_id: str) -> Optional[str]:
        return self._anon_sessions.get(game_id)

    def remember_deck(self, game_id: str, deck_id: Optional[str]) -> None:
        """Record which deck a game was started with.

        duels_get_deck_tracker needs the decklist to work out what is left, and
        making the caller repeat the id on every call is exactly the sort of
        thing an agent forgets.
        """
        if deck_id:
            self._decks[game_id] = deck_id

    def deck_for(self, game_id: str) -> Optional[str]:
        return self._decks.get(game_id)

    async def drop(self, game_id: str) -> None:
        """Close and forget one game (after conceding, abandoning or finishing)."""
        async with self._lock:
            conn = self._games.pop(game_id, None)
        if conn:
            await conn.close()

    def active_ids(self) -> list[str]:
        return list(self._games)

    async def close_all(self) -> None:
        async with self._lock:
            conns = list(self._games.values())
            self._games.clear()
        for conn in conns:
            await conn.close()
