"""MCP resources - URI-addressable reads for data that needs no arguments.

Resources suit simple template lookups; anything with validation or side
effects stays a tool.
"""

from fastmcp import Context

from ..app import app_ctx, mcp
from ..cards import summarise
from ..formatting import as_compact_json, as_json
from ..render import render_game_state, state_json


@mcp.resource("duels://cards/{definition_id}")
async def card_resource(definition_id: str, ctx: Context) -> str:
    """One Lorcana card by its catalog id (definitionId), e.g. duels://cards/10-71."""
    app = app_ctx(ctx)
    card = await app.catalog.get(definition_id)
    if not card:
        return as_json({"error": f"No card with id '{definition_id}'."})
    return as_json(card)


@mcp.resource("duels://game/{game_id}/state")
async def game_state_resource(game_id: str, ctx: Context) -> str:
    """The current state of a live game, in the same compact form as duels_get_game_state."""
    app = app_ctx(ctx)
    conn = await app.games.get(game_id, app.games.session_for(game_id))
    return as_compact_json(state_json(await render_game_state(await conn.refresh(), app.catalog)))


@mcp.resource("duels://game/{game_id}/log")
async def game_log_resource(game_id: str, ctx: Context) -> str:
    """The narrated log of a live game, with card placeholders already substituted."""
    app = app_ctx(ctx)
    conn = await app.games.get(game_id, app.games.session_for(game_id))
    await conn.refresh()
    entries = []
    for entry in conn.logs:
        message = str(entry.get("message") or "")
        for idx, ref in enumerate(entry.get("cardRefs") or []):
            message = message.replace(f"{{card:{idx}}}", str(ref.get("name") or ref.get("id")))
        entries.append({"turn": entry.get("turnNumber"), "player": entry.get("player"), "message": message})
    return as_json({"game_id": game_id, "entries": entries})


@mcp.resource("duels://deck/{deck_id}")
async def deck_resource(deck_id: str, ctx: Context) -> str:
    """A deck's contents, with each card id resolved to its name and cost."""
    app = app_ctx(ctx)
    data = await app.client.get(f"/api/decks/{deck_id}")
    deck = (data or {}).get("deck") or data or {}
    card_ids = deck.get("cardIds") or []
    resolved = await app.catalog.resolve_many(set(card_ids))
    return as_json(
        {
            "id": deck.get("id"),
            "name": deck.get("name"),
            "card_count": deck.get("cardCount"),
            "colors": deck.get("colors"),
            "valid": deck.get("valid"),
            "card_ids": card_ids,
            "cards": {k: summarise(v) for k, v in resolved.items() if v},
        }
    )
