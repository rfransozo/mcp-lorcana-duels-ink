"""Analysis tools: reading the deck and the opponent rather than making a move.

Separate from ingame.py, which is about acting. These answer the questions a
strong player asks between decisions - what is left in my deck, and what has
the other side shown me.
"""

from collections import Counter
from typing import Annotated, Optional

from fastmcp import Context
from pydantic import Field

from ..app import app_ctx, mcp
from ..cards import summarise
from ..client import DuelsError
from ..formatting import join_lines, render
from ..models import GameId, ResponseFmt, ResponseFormat
from ..toolkit import tool_errors


def _zone_ids(player: dict, *zones: str) -> list[str]:
    """definitionIds across the given zones of one player."""
    ids = []
    for zone in zones:
        for card in player.get(zone) or []:
            definition_id = card.get("definitionId")
            if definition_id:
                ids.append(str(definition_id))
    return ids


@mcp.tool(
    name="duels_get_deck_tracker",
    annotations={
        "title": "Track Your Deck and Read the Opponent",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_deck_tracker(
    ctx: Context,
    game_id: GameId,
    deck_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Which decklist you are playing. Only needed when this server did not "
                "start the game - duels_start_bot_game remembers it for you."
            ),
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns what is still in your deck, and every card the opponent has revealed so far.

    Use it to play the odds instead of guessing. It answers the two questions
    duels_get_game_state cannot: how likely is my next draw to be what I need,
    and what is the opponent actually playing.

    Your remaining cards are the decklist minus everything already visible -
    hand, board, items, inkwell and discard. The opponent side lists what they
    have put on the board or discarded, with ink colours and a cost curve, which
    is enough to read their archetype by the third or fourth turn.

    The opponent's hand and deck stay hidden; nothing here reveals them. And
    this is a set, not an order - it says what is left, never what comes next.

    Do NOT use it for the board position or for legal moves - that is
    duels_get_game_state.

    Examples:
    - "What are the odds I draw a 2-drop?" -> game_id, then read remaining_cards
    - "What is the bot playing?" -> read opponent_revealed.colors and cards
    - "Any removal left in my deck?" -> scan remaining_cards for your answers

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        deck_id (Optional[str]): Decklist id, when the game was not started here.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {
            "game_id": str,
            "my_deck": {
                "deck_id": str, "name": str,
                "total": int, "seen": int, "remaining": int,   # total = seen + remaining
                "remaining_cards": [{"definition_id","name","count","cost","type"}]
            },
            "opponent_revealed": {
                "count": int, "colors": list[str],
                "cards": [{"definition_id","name","count","cost","type"}]
            }
        }
        remaining_cards is sorted by cost then name.

    Error Handling:
        Returns an actionable error when the deck is unknown and no deck_id was
        given, naming duels_list_my_decks as the way to find one.
    """
    app = app_ctx(ctx)

    resolved_deck_id = deck_id or app.games.deck_for(game_id)
    if not resolved_deck_id:
        raise DuelsError(
            "This server does not know which deck that game is using, so it cannot work "
            "out what is left. Pass deck_id - find it with duels_list_my_decks or "
            "duels_browse_public_decks. Games started by duels_start_bot_game remember "
            "it automatically."
        )

    data = await app.client.get(f"/api/decks/{resolved_deck_id}")
    deck = (data or {}).get("deck") or data or {}
    decklist = list(deck.get("cardIds") or [])
    if not decklist:
        raise DuelsError(
            f"Deck {resolved_deck_id} came back without a card list. Check the id with "
            "duels_list_my_decks."
        )

    conn = await app.games.get(game_id, app.games.session_for(game_id))
    game = await conn.refresh()
    me = game.get("myPlayer") or {}
    opponent = game.get("opponent") or {}

    # Everything of mine that is no longer in the deck.
    seen_ids = _zone_ids(me, "hand", "field", "items", "inkwell", "discard")
    remaining = Counter(decklist)
    remaining.subtract(Counter(seen_ids))
    remaining = Counter({k: v for k, v in remaining.items() if v > 0})

    # The opponent's hand and deck are hidden; only played and discarded show.
    revealed_ids = _zone_ids(opponent, "field", "items", "discard")

    resolved = await app.catalog.resolve_many(set(remaining) | set(revealed_ids))

    def rows(counts: Counter) -> list[dict]:
        out = []
        for definition_id, count in counts.items():
            card = summarise(resolved.get(definition_id) or {}) if resolved.get(definition_id) else {}
            out.append(
                {
                    "definition_id": definition_id,
                    "name": card.get("fullName") or definition_id,
                    "count": count,
                    "cost": card.get("cost"),
                    "type": card.get("type"),
                }
            )
        return sorted(out, key=lambda r: (r["cost"] if r["cost"] is not None else 99, r["name"]))

    colors = sorted(
        {
            colour
            for definition_id in set(revealed_ids)
            for colour in ((resolved.get(definition_id) or {}).get("colors") or [])
        }
    )

    payload = {
        "game_id": game_id,
        "my_deck": {
            "deck_id": resolved_deck_id,
            "name": deck.get("name"),
            "total": len(decklist),
            # Derived from what is actually left, so total = seen + remaining
            # always holds. Counting visible cards directly would break that,
            # since the board can hold cards the decklist never contained.
            "seen": len(decklist) - sum(remaining.values()),
            "remaining": sum(remaining.values()),
            "remaining_cards": rows(remaining),
        },
        "opponent_revealed": {
            "count": len(revealed_ids),
            "colors": colors,
            "cards": rows(Counter(revealed_ids)),
        },
    }

    def md(p: dict) -> str:
        mine, theirs = p["my_deck"], p["opponent_revealed"]
        lines = [
            f"# Deck tracker - {mine.get('name') or mine['deck_id']}",
            "",
            f"**{mine['remaining']} of {mine['total']} cards left** "
            f"({mine['seen']} already drawn, played or inked)",
            "",
            "## Still in your deck",
        ]
        for row in mine["remaining_cards"]:
            cost = f"{row['cost']} ink" if row["cost"] is not None else "-"
            lines.append(f"- {row['count']}x **{row['name']}** ({cost}) `{row['definition_id']}`")

        lines += ["", f"## Opponent has shown ({theirs['count']} cards)"]
        if theirs["colors"]:
            lines.append(f"Ink: {', '.join(c.title() for c in theirs['colors'])}")
        if theirs["cards"]:
            for row in theirs["cards"]:
                cost = f"{row['cost']} ink" if row["cost"] is not None else "-"
                lines.append(f"- {row['count']}x **{row['name']}** ({cost})")
        else:
            lines.append("_Nothing yet._")
        return join_lines(lines)

    return render(payload, response_format, md)
