"""Game lifecycle: starting bot games, private tables, ranked queues."""

from typing import Annotated, Any, Optional

from fastmcp import Context
from pydantic import Field

from ..app import AppContext, app_ctx, mcp
from ..client import DuelsError
from ..formatting import as_json, bullet, join_lines, render
from ..models import BotDifficulty, DeckId, GameId, ResponseFmt, ResponseFormat, TableId
from ..render import game_state_markdown, render_game_state
from ..toolkit import tool_errors


async def _deck_card_ids(app: AppContext, deck_id: str) -> list[str]:
    """Fetch a deck's flat list of 60 definitionIds."""
    data = await app.client.get(f"/api/decks/{deck_id}")
    deck = data.get("deck") if isinstance(data, dict) else None
    deck = deck or data
    card_ids = (deck or {}).get("cardIds")
    if not card_ids:
        raise DuelsError(
            f"Deck {deck_id} has no cardIds. Check the id with duels_list_my_decks "
            "or duels_browse_public_decks."
        )
    return list(card_ids)


@mcp.tool(
    name="duels_start_bot_game",
    annotations={
        "title": "Start a Game Against the Bot",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_start_bot_game(
    ctx: Context,
    deck_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Deck to play. Use one of your decks from duels_list_my_decks, or any "
                "public deck id from duels_browse_public_decks. Required unless "
                "deck_card_ids is given."
            ),
        ),
    ] = None,
    deck_card_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "A decklist given directly as definitionIds, one entry per copy "
                "(e.g. ['10-71','10-71','10-71','10-71', ...]). Use instead of deck_id to "
                "play a list you built yourself without saving it first."
            ),
            max_length=200,
        ),
    ] = None,
    difficulty: Annotated[
        BotDifficulty,
        Field(default=BotDifficulty.NORMAL, description="Bot difficulty."),
    ] = BotDifficulty.NORMAL,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Starts a practice game against the Duels.ink AI and returns the opening game state.

    This is the fastest way to play, and the only mode that works **without an
    account** - pass deck_card_ids (or a public deck_id) and you are in. With a
    session cookie you can use your own saved decks too.

    Only one bot game can exist at a time. If one is already running this
    returns its game_id rather than failing, so you can either carry on with it
    or end it with duels_concede.

    The game opens at the coin toss. Follow with duels_get_game_state and play
    the moves it lists.

    Do NOT use this for ranked play (duels_join_matchmaking) or to play a
    friend (duels_create_table).

    Args:
        ctx (Context): Injected by FastMCP.
        deck_id (Optional[str]): A saved or public deck id.
        deck_card_ids (Optional[list[str]]): A raw decklist of definitionIds.
        difficulty (BotDifficulty): 'easy', 'normal' (default) or 'difficult'.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"game_id": str, "already_running": bool, "state": {...}} where
        state is the same structure duels_get_game_state returns.

    Examples:
        - "Play a bot game with my Tourmaline deck" -> deck_id from duels_list_my_decks
        - "Play a quick game, no account" -> deck_id of any public deck

    Error Handling:
        Returns an error naming the missing argument when neither deck_id nor
        deck_card_ids is supplied.
    """
    app = app_ctx(ctx)

    if not deck_id and not deck_card_ids:
        raise DuelsError(
            "Pass either deck_id (from duels_list_my_decks or duels_browse_public_decks) "
            "or deck_card_ids (a raw list of definitionIds)."
        )

    card_ids = deck_card_ids or await _deck_card_ids(app, deck_id)  # type: ignore[arg-type]

    body: dict[str, Any] = {"playerDeckCardIds": card_ids, "difficulty": difficulty.value}
    already = False
    try:
        result = await app.client.post("/api/game/create-bot-game", body)
    except DuelsError as exc:
        # 409 carries the id of the game already in progress - hand it back
        # rather than making the agent parse an error string.
        message = str(exc)
        if "bot_game_in_progress" not in message:
            raise
        active = await app.client.get("/api/account/active-games")
        games = (active or {}).get("games") or []
        existing = games[0].get("id") if games else None
        if not existing:
            raise
        result, already = {"gameId": existing, "sessionId": None}, True

    game_id = result.get("gameId")
    session_id = result.get("sessionId")
    if not game_id:
        raise DuelsError(f"Duels.ink did not return a game id: {result}")
    app.games.remember_session(game_id, session_id)

    conn = await app.games.get(game_id, session_id)
    state = await render_game_state(await conn.refresh(), app.catalog)

    payload = {"game_id": game_id, "already_running": already, "state": state}

    def md(p: dict) -> str:
        head = (
            f"_A bot game was already in progress; continuing `{p['game_id']}`._\n"
            if p["already_running"]
            else f"# Bot game started: `{p['game_id']}`\n"
        )
        return head + "\n" + game_state_markdown(p["state"])

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_list_active_games",
    annotations={
        "title": "List Your Active Games",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_list_active_games(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the games you can currently rejoin, plus any open table or draft pod.

    Use to pick a game back up after a break, or to find the id of a game that
    is blocking a new one from starting.

    Do NOT use for finished games - those are in duels_get_match_history.

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"games": [...], "table": {...} | null, "activeDraftPod": {...} | null,
        "connected_game_ids": [...]} - the last being the games this server
        currently holds a live socket for.
    Examples:
        - "Do I have a game running?" -> call with defaults
        - "Pick up where I left off" -> take the game_id into duels_get_game_state

    """
    app = app_ctx(ctx)
    app.client.require_auth("Listing active games")
    data = await app.client.get("/api/account/active-games")
    payload = {
        "games": (data or {}).get("games") or [],
        "table": (data or {}).get("table"),
        "activeDraftPod": (data or {}).get("activeDraftPod"),
        "connected_game_ids": app.games.active_ids(),
    }

    def md(p: dict) -> str:
        lines = ["# Active games", ""]
        if not p["games"]:
            lines.append("No games in progress.")
        for g in p["games"]:
            lines.append(f"- `{g.get('id')}` - {g.get('status', 'in progress')}")
        if p["table"]:
            lines += ["", "## Open table", "```json", as_json(p["table"]), "```"]
        if p["activeDraftPod"]:
            lines += ["", "## Draft pod", "```json", as_json(p["activeDraftPod"]), "```"]
        return join_lines(lines)

    return render(payload, response_format, md)


# =================================================================
# Private tables
# =================================================================
@mcp.tool(
    name="duels_create_table",
    annotations={
        "title": "Create a Private Table",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_create_table(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Creates a private table and returns its id and shareable invite link.

    A table seats 2-4 players, can include bot seats, and anyone with the link
    can join - no account needed on their side. Creating one requires you to be
    signed in.

    After creating, set your deck and ready up with duels_configure_table, then
    start it. The table is just a lobby; the game itself begins when it starts.

    Do NOT use this for a quick solo practice game (duels_start_bot_game is one
    call) or for ranked play (duels_join_matchmaking).

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"table_id": str, "url": str, "view": {...}} - share `url` to
        invite someone.
    Examples:
        - "Make a table to play with a friend" -> call with defaults, then share the url

    """
    app = app_ctx(ctx)
    app.client.require_auth("Creating a table")
    data = await app.client.post("/api/table/create", {"applyDefaultPreset": True})
    payload = {
        "table_id": data.get("tableId"),
        "url": data.get("url"),
        "view": data.get("view"),
    }

    def md(p: dict) -> str:
        return join_lines(
            [
                f"# Table created: `{p['table_id']}`",
                "",
                bullet("Invite link", p["url"]),
                "",
                "Next: `duels_configure_table` to pick a deck and ready up, then start it.",
            ]
        )

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_configure_table",
    annotations={
        "title": "Configure or Start a Table",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_configure_table(
    ctx: Context,
    table_id: TableId,
    action: Annotated[
        str,
        Field(
            description=(
                "What to do: 'set_deck' (needs deck_id), 'ready', 'unready', "
                "'add_bot', 'start' or 'cancel'."
            ),
        ),
    ],
    deck_id: Annotated[
        Optional[str],
        Field(default=None, description="Deck to seat with, required for action='set_deck'."),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Drives a private table through its lobby steps: pick a deck, ready up, add a bot, start.

    The usual sequence is set_deck, then ready, then start once every seat is
    ready. Starting returns the game id, which you then play with
    duels_get_game_state and the in-game tools.

    Do NOT use this on a game that has already started - table actions only
    apply to the lobby.

    Args:
        ctx (Context): Injected by FastMCP.
        table_id (str): Table UUID from duels_create_table.
        action (str): set_deck | ready | unready | add_bot | start | cancel.
        deck_id (Optional[str]): Required when action='set_deck'.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The table's updated view. For action='start' it also carries the
        new game_id.

    Examples:
        - "Use my Tourmaline deck here" -> action='set_deck', deck_id=...
        - "I am ready" -> action='ready'
        - "Start the game" -> action='start'

    Error Handling:
        Returns an error listing the accepted actions when `action` is unknown,
        and asks for deck_id when set_deck is used without one.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Configuring a table")

    mapping = {
        "set_deck": "SET_DECK",
        "ready": "SET_READY",
        "unready": "SET_UNREADY",
        "add_bot": "ADD_BOT_SEAT",
        "start": "START_GAME",
        "cancel": "CANCEL_TABLE",
    }
    key = action.strip().lower()
    if key not in mapping:
        raise DuelsError(f"action must be one of: {', '.join(mapping)}. Got {action!r}.")

    body: dict[str, Any] = {"type": mapping[key]}
    if key == "set_deck":
        if not deck_id:
            raise DuelsError("action='set_deck' needs deck_id.")
        body["deckId"] = deck_id
        body["deckCardIds"] = await _deck_card_ids(app, deck_id)

    result = await app.client.post(f"/api/table/{table_id}/action", {"action": body})
    view = await app.client.get(f"/api/table/{table_id}/view")
    payload = {"action": key, "result": result, "view": view}
    game_id = (view or {}).get("gameId") or (result or {}).get("gameId")
    if game_id:
        payload["game_id"] = game_id

    def md(p: dict) -> str:
        lines = [f"# Table {table_id} - {p['action']}", ""]
        if p.get("game_id"):
            lines.append(f"**Game started:** `{p['game_id']}` - continue with duels_get_game_state.")
            lines.append("")
        lines += ["```json", as_json(p["view"]), "```"]
        return join_lines(lines)

    return render(payload, response_format, md)


# =================================================================
# Ranked matchmaking
# =================================================================
@mcp.tool(
    name="duels_join_matchmaking",
    annotations={
        "title": "Join a Ranked Queue",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_join_matchmaking(
    ctx: Context,
    queue_id: Annotated[
        str,
        Field(
            description=(
                "Which queue to join, e.g. 'quick-play', 'core-bo1', 'core-bo3', "
                "'infinity-bo1', 'infinity-bo3', 'special-ja-bo1'."
            ),
            min_length=2,
            max_length=48,
        ),
    ],
    deck_id: DeckId,
    competitive_only: Annotated[
        bool,
        Field(default=False, description="Restrict to the competitive matchmaking pool."),
    ] = False,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Joins a ranked matchmaking queue and returns your queue position and estimated wait.

    Requires a signed-in account, and you cannot queue while another game is
    active - finish or concede it first. Joining does not block: call this,
    then poll duels_list_active_games (or call this tool again) until a match
    is found, and play it with the in-game tools.

    Note that this pairs you against **real people**. Decide deliberately
    before letting an agent play unattended in ranked queues.

    Do NOT use for practice (duels_start_bot_game) or for a private game with a
    friend (duels_create_table).

    Args:
        ctx (Context): Injected by FastMCP.
        queue_id (str): The queue to join.
        deck_id (str): The deck to queue with.
        competitive_only (bool): Competitive pool only. Default False.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"status": str, "position": int, "estimatedWait": int,
        "queueStartTime": ..., "gameId": str | null} - gameId appears once a
        match has been made.

    Examples:
        - "Queue for ranked with my best deck" -> queue_id='core-bo1', deck_id=...
        - "Play a quick unranked game" -> queue_id='quick-play', deck_id=...

    Error Handling:
        Returns "Finish your active game before joining the queue" when a game
        is already in progress.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Ranked matchmaking")

    body = {
        "queueId": queue_id,
        "deckId": deck_id,
        "deckCardIds": await _deck_card_ids(app, deck_id),
        "competitiveOnly": competitive_only,
    }
    data = await app.client.post("/api/matchmaking/join", body)

    def md(p: dict) -> str:
        if p.get("gameId"):
            return f"**Match found.** Game `{p['gameId']}` - continue with duels_get_game_state."
        return join_lines(
            [
                f"# Queued in {queue_id}",
                "",
                bullet("Status", p.get("status")),
                bullet("Position", p.get("position")),
                bullet("Estimated wait", p.get("estimatedWait")),
                "",
                "Call `duels_list_active_games` to see when a match is found.",
            ]
        )

    return render(data if isinstance(data, dict) else {"result": data}, response_format, md)


@mcp.tool(
    name="duels_leave_matchmaking",
    annotations={
        "title": "Leave the Ranked Queue",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_leave_matchmaking(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Leaves the ranked matchmaking queue you are currently waiting in.

    Safe to call when you are not queued - it simply reports that. Use it to
    stop searching without starting a game, for example when the wait is long
    or the person changes their mind.

    Do NOT use it to leave a game that has already started - once matched, the
    only way out is duels_concede, which counts as a loss. Leaving the queue
    has no such penalty.

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Whatever Duels.ink reports about leaving, typically
        {"success": bool}.
    Examples:
        - "Stop searching" -> call with defaults

    """
    app = app_ctx(ctx)
    app.client.require_auth("Leaving the queue")
    data = await app.client.post("/api/matchmaking/leave", {})
    return render(
        data if isinstance(data, dict) else {"result": data},
        response_format,
        lambda p: "Left the matchmaking queue.",
    )


@mcp.tool(
    name="duels_get_table",
    annotations={
        "title": "Get a Table's Lobby State",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_table(
    ctx: Context,
    table_id: TableId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns a table's lobby state: seats, who is ready, which decks are chosen, and whether it has started.

    Use after creating or being invited to a table, to see who has joined and
    what is still missing before it can start. If the table has already started,
    the response carries the game_id to continue with.

    Taking a seat is done by choosing a deck: call duels_configure_table with
    action='set_deck', then action='ready'.

    Do NOT use this once the table has started - switch to duels_get_game_state
    with the game_id it reports. Do NOT use it for bot practice, which needs no
    table at all (duels_start_bot_game).

    Args:
        ctx (Context): Injected by FastMCP.
        table_id (str): Table UUID, from duels_create_table or an invite link
            (the last path segment of https://duels.ink/table/<id>).
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"table_id": str, "status": str, "game_id": str | null,
        "view": {...}} - view holds the seats and configuration.
    Examples:
        - "Has anyone joined my table?" -> table_id from duels_create_table
        - "I got this table link, what is in it?" -> table_id is the last path segment of the URL

    """
    app = app_ctx(ctx)
    view = await app.client.get(f"/api/table/{table_id}/view")
    payload = {
        "table_id": table_id,
        "status": (view or {}).get("status"),
        "game_id": (view or {}).get("gameId"),
        "view": view,
    }

    def md(p: dict) -> str:
        lines = [f"# Table `{p['table_id']}`", "", bullet("Status", p["status"])]
        if p["game_id"]:
            lines.append(
                f"- **Game started**: `{p['game_id']}` - continue with duels_get_game_state"
            )
        lines += ["", "```json", as_json(p["view"])[:3000], "```"]
        return join_lines(lines)

    return render(payload, response_format, md)
