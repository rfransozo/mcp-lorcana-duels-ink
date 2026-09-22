"""Alternative game modes: puzzles, sealed, draft and the playground sandbox."""

from typing import Annotated, Any, Optional

from fastmcp import Context
from pydantic import Field

from ..app import app_ctx, mcp
from ..client import DuelsError
from ..formatting import as_json, empty_result, join_lines, render
from ..models import ResponseFmt, ResponseFormat
from ..toolkit import tool_errors


@mcp.tool(
    name="duels_get_puzzle",
    annotations={
        "title": "Get a Lorcana Puzzle",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_puzzle(
    ctx: Context,
    puzzle_id: Annotated[
        str,
        Field(
            description=(
                "The puzzle's id, taken from its URL at https://duels.ink/puzzles "
                "(duels.ink/puzzle/<id>)."
            ),
            min_length=1,
            max_length=64,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns a Lorcana puzzle: a fixed board position with a goal, such as winning this turn.

    Use to fetch a specific puzzle's setup and solve it.

    Note there is no endpoint that lists puzzles, so ids have to come from the
    puzzles page at https://duels.ink/puzzles. Do NOT guess ids.

    Examples:
    - "Solve the puzzle at duels.ink/puzzle/abc123" -> puzzle_id='abc123'

    Args:
        ctx (Context): Injected by FastMCP.
        puzzle_id (str): The puzzle id from its URL.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The puzzle document - its board setup, objective and constraints,
        as Duels.ink stores it.

    Error Handling:
        Returns "Error: Not found" for an unknown id.
    """
    app = app_ctx(ctx)
    data = await app.client.get(f"/api/puzzle/{puzzle_id}")
    return render(
        data if isinstance(data, dict) else {"puzzle": data},
        response_format,
        lambda p: join_lines([f"# Puzzle {puzzle_id}", "", "```json", as_json(p)[:6000], "```"]),
    )


@mcp.tool(
    name="duels_list_draft_decks",
    annotations={
        "title": "List Your Draft and Sealed Decks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_list_draft_decks(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the decks you built in draft and sealed events.

    These are separate from your constructed decks - duels_list_my_decks does
    not include them. Use this to find a draft pool's deck id to play with.
    Requires a session cookie.

    Do NOT use it for constructed decks (duels_list_my_decks) or for community
    lists (duels_browse_public_decks).

    Examples:
    - "What did I build in my last sealed?" -> call with defaults

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"count": int, "draft_decks": [...]}.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Listing draft decks")
    data = await app.client.get("/api/decks/draft")
    decks = (data or {}).get("draftDecks") or []
    if not decks:
        return empty_result("draft or sealed decks", "Start one with duels_create_sealed.")
    payload = {"count": len(decks), "draft_decks": decks}

    def md(p: dict) -> str:
        lines = [f"# Draft and sealed decks ({p['count']})", ""]
        for d in p["draft_decks"]:
            lines.append(f"- **{d.get('name', 'Unnamed')}** `{d.get('id')}`")
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_create_sealed",
    annotations={
        "title": "Start a Sealed Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_create_sealed(
    ctx: Context,
    packs_config: Annotated[
        list[dict],
        Field(
            description=(
                "Which packs to open, as a non-empty list of pack descriptors, e.g. "
                "[{'set': 7, 'count': 3}, {'set': 8, 'count': 3}]. The site's sealed "
                "page shows the currently offered configuration."
            ),
            min_length=1,
            max_length=12,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Opens sealed packs and creates a sealed pool you can then build a deck from.

    Sealed is a limited format: you open packs, build a deck from only those
    cards, and play it. Requires a session cookie.

    After creating a pool, find its deck with duels_list_draft_decks and play it
    with duels_start_bot_game or a table.

    Do NOT use for constructed play - duels_create_deck builds from the full
    catalog.

    Examples:
    - "Open a sealed pool from sets 7 and 8" -> packs_config=[{'set': 7, 'count': 3}, {'set': 8, 'count': 3}]

    Args:
        ctx (Context): Injected by FastMCP.
        packs_config (list[dict]): Non-empty list of pack descriptors.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The created sealed pool as Duels.ink reports it.

    Error Handling:
        Returns "packsConfig is required and must be a non-empty array" when the
        configuration is empty or malformed.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Creating a sealed event")
    data = await app.client.post("/api/sealed/create", {"packsConfig": packs_config})
    return render(
        data if isinstance(data, dict) else {"result": data},
        response_format,
        lambda p: join_lines(["# Sealed pool created", "", "```json", as_json(p)[:4000], "```"]),
    )


@mcp.tool(
    name="duels_create_playground",
    annotations={
        "title": "Create a Playground Sandbox",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_create_playground(
    ctx: Context,
    player1_deck_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Deck for player 1 - your own id or a public one. Omit for an "
                "empty board you fill with PLAYGROUND_SPAWN_CARD."
            ),
        ),
    ] = None,
    player2_deck_id: Annotated[
        Optional[str],
        Field(default=None, description="Deck for player 2. Omit for an empty board."),
    ] = None,
    game_variant: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "'coconut' to give each side a Coconut card and play to 25 lore. "
                "Omit for a normal game to 20."
            ),
            max_length=16,
        ),
    ] = None,
    first_player: Annotated[
        int,
        Field(default=1, description="Which side takes the first turn.", ge=1, le=2),
    ] = 1,
    skip_setup: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Skip the mulligan and start with empty hands. Forced when neither "
                "side has a deck, since there is nothing to draw."
            ),
        ),
    ] = False,
    name: Annotated[
        Optional[str],
        Field(default=None, description="Optional label for the sandbox.", max_length=80),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Creates a playground: a sandbox game where you control both sides and set up any position.

    The only way to exercise a format without opponents. A Coconut sandbox
    gives each side its Coconut and a 25-lore goal, so the format can be tested
    without finding three other players and hoping the table fills.

    Arrange it with duels_send_game_action and the PLAYGROUND_* actions, then
    play it with the normal in-game tools. Spawning takes the card's catalog id:
    {'type': 'PLAYGROUND_SPAWN_CARD', 'cardId': '10-16', 'zone': 'hand',
    'targetPlayer': 1, 'actingAs': 1} - zones are hand, inkwell, field, items,
    discard and deck.

    Do NOT use it for anything that should count: playground results are not
    games. Access is per account; duels_whoami reports your features.

    Examples:
    - "Sandbox to test my Coconut deck" -> player1_deck_id=..., game_variant='coconut'
    - "Empty board to test one interaction" -> call with no arguments
    - "Both sides, mine against a precon" -> player1_deck_id=..., player2_deck_id=...

    Args:
        ctx (Context): Injected by FastMCP.
        player1_deck_id (Optional[str]): Deck for player 1.
        player2_deck_id (Optional[str]): Deck for player 2.
        game_variant (Optional[str]): 'coconut' or omitted.
        first_player (int): 1 or 2. Default 1.
        skip_setup (bool): Skip the mulligan. Default False.
        name (Optional[str]): Label for the sandbox.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"game_id": str, "variant": str | null, "decks": {...}} - play it
        with duels_get_game_state.

    Error Handling:
        Says which deck could not be read when a deck id is wrong, and returns
        Duels.ink's own message when the account lacks playground access.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Creating a playground")

    # Checking the argument before the network keeps a typo from costing a
    # round-trip, and keeps the error about the typo.
    variant = (game_variant or "").strip().lower() or None
    if variant and variant != "coconut":
        raise DuelsError(
            f"game_variant must be 'coconut' or omitted. Got {game_variant!r}."
        )

    # Asking first turns a bare server refusal into a reason the caller can act
    # on - playground access is granted per account.
    access = await app.client.get("/api/playground/access")
    if isinstance(access, dict) and access.get("hasAccess") is False:
        raise DuelsError(
            "This account does not have playground access. duels_whoami reports "
            "which features are enabled."
        )

    async def deck_of(deck_id: Optional[str]) -> dict:
        if not deck_id:
            return {}
        data = await app.client.get(f"/api/decks/{deck_id}")
        deck = (data or {}).get("deck") or data or {}
        if not deck.get("cardIds"):
            raise DuelsError(
                f"Deck {deck_id} came back without a card list. Check the id with "
                "duels_list_my_decks or duels_browse_public_decks."
            )
        return deck

    one, two = await deck_of(player1_deck_id), await deck_of(player2_deck_id)

    # The endpoint takes card lists, not deck ids, and it is /create - not
    # /create-from-spec, which is the replay-analysis path and wants a
    # serialised position under `seed`. Pointed there, every call was refused
    # with 'Missing or invalid "seed"'.
    body: dict[str, Any] = {
        "firstPlayer": first_player,
        "deckOutWin": False,
        "skipSetup": bool(skip_setup or (not one.get("cardIds") and not two.get("cardIds"))),
    }
    if name:
        body["name"] = name
    if one.get("cardIds"):
        body["player1DeckCardIds"] = list(one["cardIds"])
    if two.get("cardIds"):
        body["player2DeckCardIds"] = list(two["cardIds"])
    if variant == "coconut":
        body["gameVariant"] = "coconut"
        # Each side needs one; a deck's own choice is used when it has one.
        body["player1CoconutCardId"] = one.get("coconutCardId") or "coconut-001"
        body["player2CoconutCardId"] = two.get("coconutCardId") or "coconut-002"

    data = await app.client.post("/api/playground/create", body)
    game_id = (data or {}).get("gameId") or (data or {}).get("id")
    payload = {
        "game_id": game_id,
        "variant": variant,
        "decks": {
            "player1": one.get("name") or player1_deck_id,
            "player2": two.get("name") or player2_deck_id,
        },
        "coconuts": {
            "player1": body.get("player1CoconutCardId"),
            "player2": body.get("player2CoconutCardId"),
        }
        if variant == "coconut"
        else None,
    }

    def md(p: dict) -> str:
        lines = [f"# Playground created: `{p['game_id']}`", ""]
        if p.get("variant"):
            lines.append(
                f"- **Variant**: {p['variant']} - each side has a Coconut, 25 lore to win"
            )
        for side in ("player1", "player2"):
            deck = (p.get("decks") or {}).get(side)
            coco = (p.get("coconuts") or {}).get(side)
            where = f"{deck or 'empty board'}"
            lines.append(f"- **{side}**: {where}{f' ({coco})' if coco else ''}")
        lines += [
            "",
            "Read it with `duels_get_game_state`, arrange it with "
            "`duels_send_game_action` and the PLAYGROUND_* actions, and play it "
            "with the normal in-game tools.",
        ]
        return join_lines(lines)

    return render(payload, response_format, md)
