"""Alternative game modes: puzzles, sealed, draft and the playground sandbox."""

from typing import Annotated, Any, Optional

from fastmcp import Context
from pydantic import Field

from ..app import app_ctx, mcp
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
    seed: Annotated[
        str,
        Field(
            description=(
                "Seed for the sandbox, which makes the setup reproducible. Any stable "
                "string works, e.g. 'shift-testing-1'."
            ),
            min_length=1,
            max_length=64,
        ),
    ],
    spec: Annotated[
        Optional[dict],
        Field(
            default=None,
            description=(
                "Optional starting setup - decks, board state and lore. Omit for an "
                "empty sandbox you then arrange with duels_send_game_action."
            ),
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Creates a playground: a sandbox game where you can set up any board position you like.

    Use it to test interactions, rehearse a line, or reproduce a rules question -
    cards can be spawned directly and lore, damage and exertion set by hand
    instead of having to play a real game into that position.

    Once created, drive it with the normal in-game tools, and arrange it with
    duels_send_game_action using the PLAYGROUND_* actions:
    PLAYGROUND_SPAWN_CARD, PLAYGROUND_SET_LORE, PLAYGROUND_SET_DAMAGE,
    PLAYGROUND_SET_EXERTED, PLAYGROUND_MOVE_CARD, PLAYGROUND_REMOVE_CARD,
    PLAYGROUND_IMPORT_DECK, PLAYGROUND_REORDER_DECK.

    Playground access is gated per account; duels_whoami reports your features.
    Do NOT use it for real games - results do not count.

    Examples:
    - "Set up a board to test Shift" -> seed='shift-test-1'
    - "Recreate this position" -> seed, then arrange it with duels_send_game_action

    Args:
        ctx (Context): Injected by FastMCP.
        seed (str): Reproducibility seed.
        spec (Optional[dict]): Starting setup.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"game_id": str, ...} for the sandbox game.

    Error Handling:
        Returns 'Missing or invalid "seed"' when the seed is empty, and a
        permission error when the account lacks playground access.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Creating a playground")

    access = await app.client.get("/api/playground/access")
    if isinstance(access, dict) and access.get("hasAccess") is False:
        return (
            "Error: this account does not have playground access. It is an opt-in feature "
            "on Duels.ink - enable it on the site first."
        )

    body: dict[str, Any] = {"seed": seed}
    if spec:
        body["spec"] = spec
    data = await app.client.post("/api/playground/create-from-spec", body)
    game_id = (data or {}).get("gameId") or (data or {}).get("id")
    payload = {"game_id": game_id, "raw": data}

    def md(p: dict) -> str:
        return join_lines(
            [
                f"# Playground created: `{p['game_id']}`",
                "",
                "Arrange it with `duels_send_game_action` using the PLAYGROUND_* actions, "
                "then play it with the normal in-game tools.",
            ]
        )

    return render(payload, response_format, md)
