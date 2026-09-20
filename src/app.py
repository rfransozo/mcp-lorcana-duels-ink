"""FastMCP application object, lifespan and per-tool context access.

Kept separate from server.py so the tool modules can import `mcp` without a
circular import back to the entrypoint.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from fastmcp import Context, FastMCP

from .cards import CardCatalog
from .client import DuelsClient, DuelsError
from .gamews import GameRegistry

load_dotenv()

# -----------------------------------------------------------------
# Logging — NUNCA escrever no stdout: stdout e o canal JSON-RPC do stdio.
# -----------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("DUELS_LOG_LEVEL", "INFO").upper(),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("duels_mcp")

SERVER_NAME = "duels_mcp"

INSTRUCTIONS = """\
Play and manage Disney Lorcana on Duels.ink - an unofficial, community-run \
Lorcana simulator.

Key things to know before playing:

* You never need to know Lorcana's rules. Duels.ink computes the legal moves \
server-side. Call duels_get_game_state and play only what appears under \
"legal_moves"; when a card cannot be played, the state says why.
* Cards have two different ids. `definitionId` ("10-71") identifies the card \
in the catalog; `instanceId` (a UUID) identifies one physical copy inside a \
game. In-game tools always want the instanceId.
* Bot games work without an account. Everything tied to a user - your decks, \
ranked matchmaking, tables, friends, match history - needs DUELS_SESSION_COOKIE \
to be configured.
"""


@dataclass
class AppContext:
    """Everything that lives for the whole server process."""

    client: DuelsClient
    catalog: CardCatalog
    games: GameRegistry


@asynccontextmanager
async def lifespan(_server: FastMCP):
    """Create the shared HTTP client, card catalog and game registry."""
    client = DuelsClient(cookie=os.getenv("DUELS_SESSION_COOKIE"))
    ctx = AppContext(
        client=client,
        catalog=CardCatalog(client),
        games=GameRegistry(client),
    )
    log.info(
        "duels_mcp starting (base=%s, authenticated=%s)",
        client.base_url,
        client.authenticated,
    )
    try:
        yield ctx
    finally:
        await ctx.games.close_all()
        await client.aclose()
        log.info("duels_mcp stopped")


mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan)


def app_ctx(ctx: Context) -> AppContext:
    """Fetch the shared AppContext from a tool's Context."""
    state: Any = getattr(ctx, "lifespan_context", None)
    if state is None:
        request_ctx = getattr(ctx, "request_context", None)
        state = getattr(request_ctx, "lifespan_context", None)
    if not isinstance(state, AppContext):
        raise DuelsError(
            "Internal error: the duels_mcp server context is unavailable. Restart the server."
        )
    return state


__all__ = ["mcp", "app_ctx", "AppContext", "SERVER_NAME", "log"]
