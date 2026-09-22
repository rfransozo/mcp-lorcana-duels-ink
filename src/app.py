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
from .matchmaking import Matchmaker

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

* Cards have two different ids. `definitionId` ("10-71") identifies the card \
in the catalog; `instanceId` (a UUID) identifies one physical copy inside a \
game. In-game tools always want the instanceId.
* Bot games work without an account. Everything tied to a user - your decks, \
ranked matchmaking, tables, friends, match history - needs DUELS_SESSION_COOKIE \
to be configured.

Playing legally and playing well are different skills:

* `legal_moves` tells you what is **permitted**, never what is **good**. \
Duels.ink computes legality, so you cannot make an illegal move - but choosing \
between the legal ones is yours, and that is where games are won or lost.
* **Read the card text.** Every card in the state carries its rules text and \
its keywords, and they change what is possible: Evasive can only be challenged \
by Evasive, Bodyguard must be challenged first, Ward cannot be targeted, Resist \
reduces damage. Check them before attacking or spending removal.
* **Read the opponent.** Their board and discard are visible. Ink colours and \
the cost curve tell you which deck you are facing, usually by turn three, and \
the discard says what they have already spent.
* **Track your own deck** with duels_get_deck_tracker - what is left, and \
therefore what you can still expect to draw.
* Watch the lore race in every direction. The goal is not always 20 - Coconut \
needs 25 and Pack Rush 15, and a table can set its own - so read `lore_to_win` \
from the state instead of assuming. A table seats up to four players and \
`opponents` holds all of them; the one to race is whoever is closest to the \
goal, not whoever you happen to be facing.
* **Games against people are on a clock, and it is unforgiving.** Every state \
carries one. Running it to zero does not pass the turn - it eliminates you \
where you stand, board, hand and deck gone and lore frozen, while the others \
play on. Read it before thinking long, and end your turn to get time credited \
back.
"""


@dataclass
class AppContext:
    """Everything that lives for the whole server process."""

    client: DuelsClient
    catalog: CardCatalog
    games: GameRegistry
    matchmaker: Matchmaker


@asynccontextmanager
async def lifespan(_server: FastMCP):
    """Create the shared HTTP client, card catalog and game registry."""
    client = DuelsClient(cookie=os.getenv("DUELS_SESSION_COOKIE"))
    ctx = AppContext(
        client=client,
        catalog=CardCatalog(client),
        games=GameRegistry(client),
        matchmaker=Matchmaker(client),
    )
    log.info(
        "duels_mcp starting (base=%s, authenticated=%s)",
        client.base_url,
        client.authenticated,
    )
    try:
        yield ctx
    finally:
        await ctx.matchmaker.close()
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
