"""Game lifecycle: starting bot games, private tables, ranked queues."""

from typing import Annotated, Any, Optional

from fastmcp import Context
from pydantic import Field

from ..app import AppContext, app_ctx, mcp
from ..client import DuelsError
from ..formatting import as_json, bullet, join_lines, render
from ..models import BotDifficulty, DeckId, GameId, ResponseFmt, ResponseFormat, TableId
from ..render import game_state_markdown, render_game_state, state_json
from ..toolkit import tool_errors


async def _deck(app: AppContext, deck_id: str) -> dict:
    """Fetch one deck, unwrapping the `{"deck": {...}}` envelope."""
    data = await app.client.get(f"/api/decks/{deck_id}")
    deck = data.get("deck") if isinstance(data, dict) else None
    deck = deck or data or {}
    if not deck.get("cardIds"):
        raise DuelsError(
            f"Deck {deck_id} has no cardIds. Check the id with duels_list_my_decks "
            "or duels_browse_public_decks."
        )
    return deck


async def _deck_card_ids(app: AppContext, deck_id: str) -> list[str]:
    """A deck's flat list of 60 definitionIds."""
    return list((await _deck(app, deck_id))["cardIds"])


# Every value below was read off a live table by setting it and reading it
# back. The server refuses a bad one by name - "Invalid format", "Invalid game
# mode", "Invalid timer preset", all under code `invalid_setting` - so these
# lists are enumerated rather than guessed.
TABLE_FORMATS = ("CoreConstructed", "InfinityConstructed", "Coconut")
# "Best of 3" in the panel. `bo1` is the default and clears the key.
MATCH_FORMATS = ("bo1", "bo3")
# "Free Undo" in the panel. `timed` materialises undoTimeCostSeconds: 30.
UNDO_MODES = ("unlimited", "timed", "disabled")
TABLE_MODES = ("constructed", "sealed", "draft")
TIMER_PRESETS = ("none", "standard", "blitz", "casual")
VISIBILITIES = ("public", "private")

# Short spellings people actually say, mapped to what the API wants. "Core"
# and "Infinity" on their own are refused.
FORMAT_ALIASES = {
    "core": "CoreConstructed",
    "coreconstructed": "CoreConstructed",
    "infinity": "InfinityConstructed",
    "infinityconstructed": "InfinityConstructed",
    "coconut": "Coconut",
}


def _format_name(value: str) -> str:
    """Accept the name a player would say, send the one the API wants."""
    key = "".join(c for c in value if c.isalnum()).lower()
    if key not in FORMAT_ALIASES:
        raise DuelsError(
            f"game_format must be one of: {', '.join(TABLE_FORMATS)} "
            f"(or the short forms Core, Infinity, Coconut). Got {value!r}."
        )
    return FORMAT_ALIASES[key]


def _table_view(data: object) -> dict:
    """The table itself, out of whatever wrapper it arrived in.

    `GET /api/table/{id}/view` answers `{"view": {...}}` and an action answers
    `{"success": ..., "view": {...}}`, so the table is always one level down.
    Reading the outer dict gives a status of None and a table that looks empty
    rather than unreachable - which is what this did until a real table was
    opened and every seat came back blank.
    """
    if not isinstance(data, dict):
        return {}
    inner = data.get("view")
    if isinstance(inner, dict):
        return inner
    # An action's bare acknowledgement - {"success": true} with no view - is
    # not a table, and treating it as one silently drops the game id that only
    # the real view carries.
    looks_like_a_table = any(k in data for k in ("seats", "config", "status"))
    return data if looks_like_a_table else {}


def _table_payload(table_id: str, view: dict) -> dict:
    config = view.get("config") or {}
    seats = [
        {
            "index": seat.get("index"),
            "player": seat.get("username"),
            "is_bot": bool(seat.get("isBot")),
            "is_you": seat.get("index") == view.get("mySeatIndex"),
            "connected": seat.get("connected"),
            "ready": bool(seat.get("ready")),
            "has_deck": bool(seat.get("hasDeck")),
            "coconut_card_id": seat.get("coconutCardId"),
            "has_coconut": bool(seat.get("hasCoconut")),
        }
        for seat in view.get("seats") or []
    ]
    max_seats = config.get("maxSeats")
    return {
        "table_id": table_id,
        "status": view.get("status"),
        "game_id": view.get("gameId"),
        "format": config.get("gameFormat"),
        # Sent as `gameMode`, stored as `deckType`. Sealed and Draft are 1v1.
        "game_mode": config.get("deckType"),
        "max_seats": max_seats,
        "open_seats": config.get("openSeats") or max_seats,
        "visibility": config.get("visibility"),
        "timer_preset": config.get("timerPreset"),
        "lore_to_win": config.get("loreToWin"),
        # Absent means Best of 1: the server stores nothing for the default.
        "match_format": config.get("matchFormat") or "bo1",
        "undo_mode": (config.get("privateUndoConfig") or {}).get("mode"),
        "allow_spectators": config.get("allowSpectators"),
        "reveal_hands": config.get("revealHands"),
        "reveal_hands_to_spectators": config.get("revealHandsToSpectators"),
        "afk_ping": config.get("afkPingEnabled"),
        "allow_guest_invites": config.get("allowGuestInvites"),
        "is_host": view.get("isHost"),
        "is_spectator": view.get("isSpectator"),
        "my_seat": view.get("mySeatIndex"),
        "seats": seats,
        "seats_filled": len(seats),
        "spectators": [s.get("username") for s in view.get("spectators") or []],
    }


def _table_md(p: dict) -> str:
    """The lobby as a table of seats.

    The raw view carries the whole join/leave history and every seat's sixty
    card ids - thousands of tokens of nothing. One busy table had logged fifty
    seat changes before anybody sat down.
    """
    head = [
        f"# Table `{p['table_id']}`",
        "",
        bullet("Status", p.get("status")),
        bullet("Format", p.get("format")),
        bullet("Seats", f"{p['seats_filled']}/{p.get('open_seats') or p['seats_filled']}"),
    ]
    if p.get("visibility"):
        head.append(bullet("Visibility", p["visibility"]))
    if p.get("game_mode") and p["game_mode"] != "constructed":
        head.append(bullet("Mode", f"{p['game_mode']} (1v1 only)"))
    if p.get("timer_preset") and p["timer_preset"] != "none":
        head.append(bullet("Timer", p["timer_preset"]))
    if p.get("match_format") and p["match_format"] != "bo1":
        head.append(bullet("Match", "best of three"))
    if p.get("undo_mode"):
        head.append(bullet("Undo", p["undo_mode"]))
    if p.get("reveal_hands"):
        head.append(bullet("Open hand", "every hand is visible to everyone"))
    if p.get("lore_to_win"):
        head.append(bullet("Lore to win", p["lore_to_win"]))
    if p.get("allow_spectators") is False:
        head.append(bullet("Spectators", "not allowed"))
    if p.get("game_id"):
        head += [
            "",
            f"**Game started:** `{p['game_id']}` - continue with duels_get_game_state.",
        ]

    rows = [
        "",
        "| Seat | Player | Here | Deck | Coconut | Ready |",
        "|---|---|---|---|---|---|",
    ]
    for seat in p["seats"]:
        who = seat["player"] or "?"
        if seat["is_you"]:
            who = f"**{who}** (you)"
        elif seat["is_bot"]:
            who = f"{who} (bot)"
        coconut = seat["coconut_card_id"] or ("yes" if seat["has_coconut"] else "-")
        # A seat keeps its name, deck and ready flag after its player has gone,
        # so without this an abandoned table looks exactly like a full one.
        here = {True: "yes", False: "**gone**", None: "?"}[seat.get("connected")]
        rows.append(
            f"| {seat['index']} | {who} | {here} | {'yes' if seat['has_deck'] else '-'} | "
            f"{coconut} | {'ready' if seat['ready'] else 'not ready'} |"
        )
    if p["seats"] and all(seat.get("connected") is False for seat in p["seats"]):
        rows += ["", "_Nobody is connected - this table has most likely been abandoned._"]
    empty = (p.get("open_seats") or 0) - p["seats_filled"]
    if empty > 0:
        rows += ["", f"_{empty} seat(s) still open._"]
    if p.get("spectators"):
        rows += ["", f"_Watching: {', '.join(p['spectators'])}._"]
    return join_lines([*head, *rows])


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

    Do NOT use this for ranked play (duels_matchmaking action='join') or to play a
    friend (duels_create_table).

    Examples:
    - "Play a bot game with my Tourmaline deck" -> deck_id from duels_list_my_decks
    - "Play a quick game, no account" -> deck_id of any public deck

    Args:
        ctx (Context): Injected by FastMCP.
        deck_id (Optional[str]): A saved or public deck id.
        deck_card_ids (Optional[list[str]]): A raw decklist of definitionIds.
        difficulty (BotDifficulty): 'easy', 'normal' (default) or 'difficult'.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"game_id": str, "already_running": bool, "state": {...}} where
        state is the same structure duels_get_game_state returns.

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
    app.games.remember_deck(game_id, deck_id)

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

    return render(payload, response_format, md,
                  json_view=lambda p: {**p, "state": state_json(p["state"])})


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

    Examples:
    - "Do I have a game running?" -> call with defaults
    - "Pick up where I left off" -> take the game_id into duels_get_game_state

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"games": [...], "table": {...} | null, "activeDraftPod": {...} | null,
        "connected_game_ids": [...]} - the last being the games this server
        currently holds a live socket for.
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
    game_format: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "'Coconut', 'Core' or 'Infinity'. The table's format decides which "
                "decks are legal in it, and Coconut is table-only - there is no queue "
                "and no bot for it. Defaults to Core."
            ),
            max_length=32,
        ),
    ] = None,
    seats: Annotated[
        Optional[int],
        Field(
            default=None,
            description=(
                "How many players can join, 2 to 4. More than 2 needs the default "
                "Constructed mode - Sealed and Draft are 1v1."
            ),
            ge=2,
            le=4,
        ),
    ] = None,
    public: Annotated[
        Optional[bool],
        Field(
            default=None,
            description=(
                "True lists the table in the public lobby so strangers can find it. "
                "False (the default) makes it reachable by invite link only."
            ),
        ),
    ] = None,
    timer_preset: Annotated[
        Optional[str],
        Field(
            default=None,
            description="'none' (default), 'casual', 'standard' or 'blitz'.",
            max_length=16,
        ),
    ] = None,
    lore_to_win: Annotated[
        Optional[int],
        Field(
            default=None,
            description=(
                "Override the lore goal, 1 to 99. Leave unset for the format's own: "
                "20 normally, 25 in Coconut."
            ),
            ge=1,
            le=99,
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Creates a private table and returns its id and shareable invite link.

    A table seats 2-4 players, can include bot seats, and anyone with the link
    can join - no account needed on their side. Creating one requires you to be
    signed in. It is also the only way into Coconut and the only way into a
    game with more than one opponent.

    Settings can be given here or changed later with duels_configure_table.
    After creating, set your deck and ready up, then start it. The table is
    just a lobby; the game itself begins when it starts.

    Do NOT use this for a quick solo practice game (duels_start_bot_game is one
    call) or for ranked play (duels_matchmaking action='join').

    Examples:
    - "Make a table to play with a friend" -> call with defaults, share the url
    - "Open a 4-player Coconut table anyone can join" ->
      game_format='Coconut', seats=4, public=True
    - "A timed 1v1" -> timer_preset='standard'

    Args:
        ctx (Context): Injected by FastMCP.
        game_format (Optional[str]): Coconut, Core or Infinity.
        seats (Optional[int]): 2 to 4.
        public (Optional[bool]): List it in the public lobby.
        timer_preset (Optional[str]): none | casual | standard | blitz.
        lore_to_win (Optional[int]): 1 to 99.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"table_id": str, "url": str, ...} plus the lobby state as
        duels_get_table returns it. Share `url` to invite someone.

    Error Handling:
        Names the accepted values when a format or timer preset is not one of
        them, rather than letting Duels.ink answer 400.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Creating a table")

    # Creation takes its settings at the top level. A nested `config` object is
    # accepted and silently dropped, which is the worst of both worlds.
    body: dict[str, Any] = {"applyDefaultPreset": True}
    if game_format:
        body["gameFormat"] = _format_name(game_format)
    if timer_preset:
        preset = timer_preset.strip().lower()
        if preset not in TIMER_PRESETS:
            raise DuelsError(
                f"timer_preset must be one of: {', '.join(TIMER_PRESETS)}. "
                f"Got {timer_preset!r}."
            )
        body["timerPreset"] = preset
    if public is not None:
        body["visibility"] = "public" if public else "private"
    if lore_to_win is not None:
        body["loreToWin"] = lore_to_win

    data = await app.client.post("/api/table/create", body)
    table_id = data.get("tableId")

    # `maxSeats` is fixed at 4 and ignored wherever it is sent; `openSeats` is
    # the only thing that decides how many people can actually join, and it is
    # not honoured at creation either.
    if seats is not None and table_id:
        await app.client.post(
            f"/api/table/{table_id}/action",
            {"action": {"type": "UPDATE_SETTINGS", "config": {"openSeats": seats}}},
        )

    view = await app.tables.attend(table_id) if table_id else None
    if view is None:
        view = _table_view(data.get("view")) or _table_view(
            await app.client.get(f"/api/table/{table_id}/view")
        )
    payload = {**_table_payload(table_id, view), "url": data.get("url")}

    def md(p: dict) -> str:
        return join_lines(
            [
                bullet("Invite link", p["url"]),
                "",
                _table_md(p),
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
                "'add_bot', 'kick_seat' (needs seat_index), 'set_format' (needs "
                "game_format), 'set_seats' (needs seats), 'make_public', "
                "'make_private', 'start' or 'cancel'."
            ),
        ),
    ],
    deck_id: Annotated[
        Optional[str],
        Field(default=None, description="Deck to seat with, required for action='set_deck'."),
    ] = None,
    seat_index: Annotated[
        Optional[int],
        Field(
            default=None,
            description=(
                "Which seat to remove, for action='kick_seat'. Seat numbers are in "
                "duels_get_table. This is also how a bot seat is removed."
            ),
            ge=0,
            le=7,
        ),
    ] = None,
    game_format: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='set_format': 'Core', 'Infinity' or 'Coconut'. "
                "The table's format decides which decks are legal in it - a three-ink "
                "Coconut deck is refused at a Core table."
            ),
            max_length=24,
        ),
    ] = None,
    seats: Annotated[
        Optional[int],
        Field(
            default=None,
            description=(
                "For action='set_seats': how many players can join, 2 to 4. More "
                "than 2 needs Constructed mode - Sealed and Draft are 1v1."
            ),
            ge=2,
            le=4,
        ),
    ] = None,
    timer_preset: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='set_timer': 'none', 'casual', 'standard' or 'blitz'. "
                "A timed game eliminates a player who runs their clock to zero."
            ),
            max_length=16,
        ),
    ] = None,
    lore_to_win: Annotated[
        Optional[int],
        Field(
            default=None,
            description="For action='set_lore_goal': the lore target, 1 to 99.",
            ge=1,
            le=99,
        ),
    ] = None,
    game_mode: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='set_mode': 'constructed' (the default, and the only "
                "one that reaches 3-4 players), 'sealed' or 'draft'."
            ),
            max_length=16,
        ),
    ] = None,
    match_format: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='set_match_format': 'bo1' (single game, the default) "
                "or 'bo3' (best of three)."
            ),
            max_length=8,
        ),
    ] = None,
    undo_mode: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='set_undo': 'unlimited' (free undo for everyone), "
                "'timed' (each undo costs 30 seconds of your clock) or 'disabled'."
            ),
            max_length=16,
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Drives a private table through its lobby steps: pick a deck, ready up, add a bot, start.

    The usual sequence is set_deck, then ready, then start once every seat is
    ready. Starting returns the game id, which you then play with
    duels_get_game_state and the in-game tools.

    Do NOT use this on a game that has already started - table actions only
    apply to the lobby.

    Examples:
    - "Use my Tourmaline deck here" -> action='set_deck', deck_id=...
    - "I am ready" -> action='ready'
    - "Start the game" -> action='start'
    - "Get me out of this table" -> action='leave'

    Args:
        ctx (Context): Injected by FastMCP.
        table_id (str): Table UUID from duels_create_table.
        action (str): set_deck | ready | unready | add_bot | kick_seat |
            set_format | set_mode | set_seats | set_timer | set_lore_goal |
            set_match_format | set_undo | make_public | make_private | leave |
            start | cancel.
        deck_id (Optional[str]): Required when action='set_deck'.
        seat_index (Optional[int]): Required when action='kick_seat'.
        game_format (Optional[str]): Required when action='set_format'.
        seats (Optional[int]): Required when action='set_seats'.
        timer_preset (Optional[str]): Required when action='set_timer'.
        lore_to_win (Optional[int]): Required when action='set_lore_goal'.
        game_mode (Optional[str]): Required when action='set_mode'.
        match_format (Optional[str]): Required when action='set_match_format'.
        undo_mode (Optional[str]): Required when action='set_undo'.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The table's updated view. For action='start' it also carries the
        new game_id.

    Error Handling:
        Returns an error listing the accepted actions when `action` is unknown,
        and asks for deck_id when set_deck is used without one.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Configuring a table")

    mapping = {
        "set_deck": "SET_DECK",
        "ready": "SET_READY",
        "unready": "SET_READY",
        "add_bot": "ADD_BOT_SEAT",
        "leave": "LEAVE_TABLE",
        "kick_seat": "KICK_SEAT",
        "set_format": "UPDATE_SETTINGS",
        "set_mode": "UPDATE_SETTINGS",
        "set_seats": "UPDATE_SETTINGS",
        "set_timer": "UPDATE_SETTINGS",
        "set_lore_goal": "UPDATE_SETTINGS",
        "set_match_format": "UPDATE_SETTINGS",
        "set_undo": "UPDATE_SETTINGS",
        "make_public": "UPDATE_SETTINGS",
        "make_private": "UPDATE_SETTINGS",
        # START_GAME is refused as "Invalid table action" - the site has
        # always sent START_TABLE. Nothing caught it because the table
        # stayed in `assembling` and simply looked like it was waiting.
        "start": "START_TABLE",
        "cancel": "CANCEL_TABLE",
    }
    key = action.strip().lower()
    if key not in mapping:
        raise DuelsError(f"action must be one of: {', '.join(mapping)}. Got {action!r}.")

    body: dict[str, Any] = {"type": mapping[key]}
    if key in ("ready", "unready"):
        # SET_READY carries the value; there is no SET_UNREADY. Sending the
        # bare type is answered 200 and changes nothing, so the seat stays
        # unready while the call reports success.
        body["ready"] = key == "ready"
    elif key == "set_deck":
        if not deck_id:
            raise DuelsError("action='set_deck' needs deck_id.")
        deck = await _deck(app, deck_id)
        body["deckId"] = deck_id
        body["deckCardIds"] = list(deck["cardIds"])
        # The seat does not infer the Coconut from the deck id: sending only
        # the cards seats a Coconut deck without its Coconut, and the seat
        # reports hasCoconut false while still looking seated and ready.
        if deck.get("coconutCardId"):
            body["coconutCardId"] = deck["coconutCardId"]
    elif key == "kick_seat":
        if seat_index is None:
            raise DuelsError(
                "action='kick_seat' needs seat_index. Seat numbers are in duels_get_table."
            )
        body["seatIndex"] = seat_index
    elif key == "set_format":
        if not game_format:
            raise DuelsError(
                "action='set_format' needs game_format: Core, Infinity or Coconut."
            )
        body["config"] = {"gameFormat": _format_name(game_format)}
    elif key == "set_mode":
        mode = (game_mode or "").strip().lower()
        if mode not in TABLE_MODES:
            raise DuelsError(
                f"action='set_mode' needs game_mode, one of: {', '.join(TABLE_MODES)}. "
                "Sealed and Draft are 1v1; only Constructed reaches 3-4 players."
            )
        body["config"] = {"gameMode": mode}
    elif key == "set_seats":
        if seats is None:
            raise DuelsError("action='set_seats' needs seats (2 to 4).")
        # `maxSeats` is fixed at 4: every value from 1 to 8 is answered 200 and
        # none of them is stored. `openSeats` is the only lever that exists,
        # and asking for more than 2 at a Sealed or Draft table is refused with
        # "This game mode is 1v1 only".
        body["config"] = {"openSeats": seats}
    elif key == "set_timer":
        preset = (timer_preset or "").strip().lower()
        if preset not in TIMER_PRESETS:
            raise DuelsError(
                f"action='set_timer' needs timer_preset, one of: "
                f"{', '.join(TIMER_PRESETS)}."
            )
        body["config"] = {"timerPreset": preset}
    elif key == "set_lore_goal":
        if lore_to_win is None:
            raise DuelsError("action='set_lore_goal' needs lore_to_win (1 to 99).")
        body["config"] = {"loreToWin": lore_to_win}
    elif key == "set_match_format":
        fmt = (match_format or "").strip().lower()
        if fmt not in MATCH_FORMATS:
            raise DuelsError(
                f"action='set_match_format' needs match_format, one of: "
                f"{', '.join(MATCH_FORMATS)}."
            )
        body["config"] = {"matchFormat": fmt}
    elif key == "set_undo":
        mode = (undo_mode or "").strip().lower()
        if mode not in UNDO_MODES:
            raise DuelsError(
                f"action='set_undo' needs undo_mode, one of: {', '.join(UNDO_MODES)}."
            )
        # The seconds are the server's to fill in: sending them alongside is
        # refused as "Invalid undo settings".
        body["config"] = {"privateUndoConfig": {"mode": mode}}
    elif key in ("make_public", "make_private"):
        body["config"] = {
            "visibility": "public" if key == "make_public" else "private"
        }

    result = await app.client.post(f"/api/table/{table_id}/action", {"action": body})

    # Once we are out of the lobby there is nothing to be present at, and a
    # socket left open on a cancelled table just fails on its next ping.
    if key in ("leave", "cancel", "start"):
        await app.tables.drop(table_id)
    else:
        await app.tables.attend(table_id)

    view = _table_view(result) or _table_view(
        await app.client.get(f"/api/table/{table_id}/view")
    )
    payload = _table_payload(table_id, view)
    payload["action"] = key

    def md(p: dict) -> str:
        return join_lines([f"_{p['action']} done._", "", _table_md(p)])

    return render(payload, response_format, md)


# =================================================================
# Ranked matchmaking
# =================================================================
@mcp.tool(
    name="duels_matchmaking",
    annotations={
        "title": "Ranked Queue: Join, Wait, Leave",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_matchmaking(
    ctx: Context,
    action: Annotated[
        str,
        Field(
            default="wait",
            description=(
                "'join' enters a queue (needs queue_id and deck_id); 'wait' "
                "blocks until an opponent is found; 'leave' stops searching."
            ),
            max_length=8,
        ),
    ] = "wait",
    queue_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='join': 'quick-play', 'core-bo1', 'core-bo3', "
                "'infinity-bo1', 'infinity-bo3', 'special-ja-bo1'."
            ),
            max_length=48,
        ),
    ] = None,
    deck_id: Annotated[
        Optional[str],
        Field(default=None, description="For action='join': the deck to queue with."),
    ] = None,
    competitive_only: Annotated[
        bool,
        Field(default=False, description="Restrict to the competitive pool."),
    ] = False,
    timeout_seconds: Annotated[
        int,
        Field(
            default=120,
            description="For action='wait': how long to block before giving up.",
            ge=5,
            le=600,
        ),
    ] = 120,
    on_match_choose: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For action='wait': answer the coin toss the moment the match "
                "lands, without a second round trip. 'play' goes first and skips "
                "the first draw; 'draw' goes second and draws on turn one. Omit "
                "to decide yourself - but the toss is on a ~15 second clock."
            ),
            max_length=8,
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Runs the ranked queue end to end: join it, wait in it, or stop searching.

    One tool because it is one piece of state. Joining does not find anybody by
    itself - the queue entry only survives while a heartbeat runs, and that
    heartbeat runs while you wait. So the sequence is join, then wait, and
    waiting again after a timeout is normal.

    Note that this pairs you with **real people**. Decide deliberately before
    letting an agent play unattended, and once a match returns, play promptly.

    Do NOT re-join to poll: that re-enters the queue, sends you to the back,
    and can undo a pairing already in progress. Use action='wait'. And do NOT
    use action='leave' on a game that has already started - the only way out of
    one is duels_concede, which counts as a loss.

    Examples:
    - "Queue for ranked" -> action='join', queue_id='core-bo1', deck_id=...
    - "Wait for my opponent, and go first" -> action='wait', on_match_choose='play'
    - "Wait for my opponent" -> action='wait'
    - "Keep waiting, it is slow" -> action='wait', timeout_seconds=300
    - "Stop searching" -> action='leave'

    Args:
        ctx (Context): Injected by FastMCP.
        action (str): join | wait | leave.
        queue_id (Optional[str]): Required when action='join'.
        deck_id (Optional[str]): Required when action='join'.
        competitive_only (bool): Competitive pool only. Default False.
        timeout_seconds (int): 5-600, default 120. Used by action='wait'.
        on_match_choose (Optional[str]): 'play' or 'draw', answered the moment
            the match lands.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: join -> {"status", "position", "estimatedWait", "gameId"};
        wait -> {"game_id", "beats"} or {"timed_out", "queue_id", "beats"};
        leave -> {"success": bool}.

    Error Handling:
        Says plainly when you ask to wait without being queued, and returns
        "Finish your active game before joining the queue" when one is running.
    """
    app = app_ctx(ctx)
    wanted = (action or "wait").strip().lower()
    if wanted not in ("join", "wait", "leave"):
        raise DuelsError(f"action must be join, wait or leave. Got {action!r}.")
    choice = (on_match_choose or "").strip().lower()
    if choice and choice not in ("play", "draw"):
        raise DuelsError(
            f"on_match_choose must be 'play' or 'draw'. Got {on_match_choose!r}."
        )
    app.client.require_auth("Ranked matchmaking")

    if wanted == "leave":
        app.matchmaker.stop()
        data = await app.client.post("/api/matchmaking/leave", {})
        return render(
            data if isinstance(data, dict) else {"result": data},
            response_format,
            lambda p: "Left the matchmaking queue." if p.get("success", True) else str(p),
        )

    if wanted == "wait":
        if not app.matchmaker.queued:
            raise DuelsError(
                "You are not in a queue, so there is nothing to wait for. Join "
                "one with action='join' first."
            )
        result = await app.matchmaker.wait_for_match(timeout_seconds)

        if choice and result.get("game_id"):
            # The toss carries its own clock, and it is the shortest in the
            # game: ten to fifteen seconds. Reading the state first and
            # answering second spends all of it, and the server picks for you -
            # which is exactly how a ranked game began on somebody else's
            # choice. Declared up front, the answer goes out in the same call
            # the pairing arrives in.
            result["starting_player_choice"] = choice
            try:
                conn = await app.games.get(result["game_id"])
                await conn.send_action(
                    {"type": "CHOOSE_STARTING_PLAYER", "choice": choice}
                )
                result["starting_player_sent"] = True
            except DuelsError as exc:
                # Losing the toss is not losing the match: the game is playable
                # either way, so this reports rather than raises.
                result["starting_player_sent"] = False
                result["starting_player_error"] = str(exc)

        def waited(p: dict) -> str:
            if p.get("game_id"):
                lines = [f"**Match found.** Game `{p['game_id']}`", ""]
                if p.get("starting_player_sent"):
                    going = "first" if p["starting_player_choice"] == "play" else "second"
                    lines += [f"_Coin toss answered: you go {going}._", ""]
                elif p.get("starting_player_error"):
                    lines += [
                        f"_The coin toss could not be answered ({p['starting_player_error']})_",
                        "",
                    ]
                lines.append(
                    "There is a real person waiting - play with duels_get_game_state."
                )
                return join_lines(lines)
            return join_lines([
                f"Still queued in {p.get('queue_id')} after {timeout_seconds}s "
                f"({p.get('beats')} heartbeats sent).",
                "",
                "That is not an error. Call this again to keep waiting.",
            ])

        return render(result, response_format, waited)

    if not queue_id or not deck_id:
        raise DuelsError(
            "action='join' needs queue_id and deck_id. Queues: quick-play, "
            "core-bo1, core-bo3, infinity-bo1, infinity-bo3, special-ja-bo1."
        )

    data = await app.client.post(
        "/api/matchmaking/join",
        {
            "queueId": queue_id,
            "deckId": deck_id,
            "deckCardIds": await _deck_card_ids(app, deck_id),
            "competitiveOnly": competitive_only,
        },
    )

    # Joining alone never matches anyone: the entry has to be kept alive with a
    # heartbeat, which the site sends about once a second for as long as it is
    # queued. See src/matchmaking.py for how that was found.
    app.matchmaker.start(queue_id, deck_id)

    def joined(p: dict) -> str:
        if p.get("gameId"):
            return f"**Match found.** Game `{p['gameId']}` - continue with duels_get_game_state."
        return join_lines([
            f"# Queued in {queue_id}",
            "",
            bullet("Status", p.get("status")),
            bullet("Position", p.get("position")),
            bullet("Estimated wait", p.get("estimatedWait")),
            "",
            "Staying in the queue now - a heartbeat is running.",
            "Call this again with action='wait' to block until an opponent is found.",
            "Do NOT re-join to poll: it sends you to the back of the queue.",
        ])

    return render(data if isinstance(data, dict) else {"result": data}, response_format, joined)


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

    Examples:
    - "Has anyone joined my table?" -> table_id from duels_create_table
    - "I got this table link, what is in it?" -> table_id is the last path segment of the URL

    Args:
        ctx (Context): Injected by FastMCP.
        table_id (str): Table UUID, from duels_create_table or an invite link
            (the last path segment of https://duels.ink/table/<id>).
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"table_id", "status", "game_id", "format", "max_seats",
        "open_seats", "visibility", "my_seat", "seats": [{"index", "player",
        "is_bot", "is_you", "ready", "has_deck", "coconut_card_id"}],
        "spectators"}.
    """
    app = app_ctx(ctx)
    # Reading a table also sits in it: `connected` on a seat means "holds a
    # socket", so without one our own chair reads as empty to everybody else.
    view = await app.tables.attend(table_id)
    if view is None:
        view = _table_view(await app.client.get(f"/api/table/{table_id}/view"))
    return render(_table_payload(table_id, view), response_format, _table_md)


@mcp.tool(
    name="duels_list_open_tables",
    annotations={
        "title": "List Open Tables",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_list_open_tables(
    ctx: Context,
    game_format: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Only tables of this format, e.g. 'Coconut', 'Core', 'Infinity'. "
                "Matched case-insensitively. Omit for every open table."
            ),
            max_length=24,
        ),
    ] = None,
    with_space: Annotated[
        bool,
        Field(default=True, description="Only tables that still have a free seat."),
    ] = True,
    untimed_only: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Only tables with no turn timer. A timed turn is two minutes of "
                "real time and it is spent whether or not anyone is thinking, so "
                "an untimed table is the safe one when latency is unpredictable."
            ),
        ),
    ] = False,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the public tables anyone can join right now, with their format and free seats.

    This is how to find a game in a format the bot cannot play. Coconut and
    multiplayer are both table-only - there is no queue for either and no
    practice mode - so an open table is the only way into one. Join with
    duels_join_table.

    Do NOT use it to find your own table: that is in duels_list_active_games.
    Tables shared by invite link only are not listed; make yours public with
    duels_configure_table action='make_public'.

    Examples:
    - "Find a Coconut game" -> game_format='Coconut'
    - "Any table with room?" -> call with defaults
    - "Show every open table, full ones too" -> with_space=False

    Args:
        ctx (Context): Injected by FastMCP.
        game_format (Optional[str]): Filter by format.
        with_space (bool): Only tables with a free seat. Default True.
        untimed_only (bool): Skip tables with a turn timer. Default False.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"count": int, "tables": [{"table_id", "host", "format",
        "seats_filled", "max_seats", "seats_open", "timer_preset",
        "last_active"}]}.
    """
    app = app_ctx(ctx)
    data = await app.client.get("/api/home/data")
    tables = []
    for row in (data or {}).get("openTables") or []:
        filled = row.get("seatsFilled") or 0
        cap = row.get("maxSeats") or 0
        fmt = row.get("format")
        # The lobby reports "CoreConstructed" where a person says "Core", so
        # an exact match would silently return nothing for the commonest word.
        if game_format:
            wanted = "".join(c for c in game_format if c.isalnum()).lower()
            wanted = FORMAT_ALIASES.get(wanted, game_format.strip())
            if str(fmt or "").lower() != wanted.lower():
                continue
        if with_space and cap and filled >= cap:
            continue
        timer = row.get("timerPreset")
        if untimed_only and timer not in (None, "none"):
            continue
        tables.append(
            {
                "table_id": row.get("id"),
                "host": row.get("hostName"),
                "format": fmt,
                "seats_filled": filled,
                "max_seats": cap,
                "seats_open": max(0, cap - filled),
                "timer_preset": timer,
                "timed": timer not in (None, "none"),
                "last_active": row.get("lastActiveAt"),
            }
        )

    if not tables:
        what = f"open {game_format} tables" if game_format else "open tables"
        return (
            f"No {what} right now. Open one with duels_create_table, then "
            "duels_configure_table action='make_public' so others can find it."
        )

    payload = {"count": len(tables), "tables": tables}

    def md(p: dict) -> str:
        lines = [f"# Open tables ({p['count']})", ""]
        for t in p["tables"]:
            timer = (
                "" if t["timer_preset"] in (None, "none") else f", {t['timer_preset']} timer"
            )
            lines.append(
                f"- **{t['host']}** - {t['format']}, "
                f"{t['seats_filled']}/{t['max_seats']} seats{timer}"
            )
            lines.append(f"  `{t['table_id']}`")
        lines += ["", "Join one with `duels_join_table`."]
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_join_table",
    annotations={
        "title": "Join a Table",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_join_table(
    ctx: Context,
    table_id: TableId,
    deck_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Deck to sit down with - your own id, or a public one from "
                "duels_browse_public_decks (starter, suggested and precon decks are "
                "all there). Its Coconut travels with it. Omit to take the seat now "
                "and choose a deck after with duels_configure_table."
            ),
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Takes a seat at an open table, optionally seating a deck at the same time.

    Use it on a table from duels_list_open_tables or on an invite link. The
    table starts once every seated player is ready, so follow with
    duels_configure_table action='ready'.

    Do NOT use it on your own table - creating one already seats you. Do NOT
    use it to watch a game: joining takes a playing seat.

    The deck has to be legal in the table's format: a three-ink Coconut deck is
    refused at a Core table, and a deck with no Coconut card cannot sit at a
    Coconut one.

    Examples:
    - "Join that Coconut table with my Coconut deck" -> table_id, deck_id
    - "Grab a seat, I'll pick a deck after" -> table_id

    Args:
        ctx (Context): Injected by FastMCP.
        table_id (str): Table UUID, from duels_list_open_tables or the last path
            segment of https://duels.ink/table/<id>.
        deck_id (Optional[str]): Deck to seat with.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The table's lobby state, as duels_get_table returns it.

    Error Handling:
        Returns Duels.ink's own reason when the table is full, already started,
        private, or the deck is illegal in its format.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Joining a table")

    result = await app.client.post(
        f"/api/table/{table_id}/action", {"action": {"type": "JOIN_TABLE"}}
    )
    if deck_id:
        deck = await _deck(app, deck_id)
        seat: dict[str, Any] = {
            "type": "SET_DECK",
            "deckId": deck_id,
            "deckCardIds": list(deck["cardIds"]),
        }
        if deck.get("coconutCardId"):
            seat["coconutCardId"] = deck["coconutCardId"]
        result = await app.client.post(f"/api/table/{table_id}/action", {"action": seat})

    await app.tables.attend(table_id)
    view = _table_view(result) or _table_view(
        await app.client.get(f"/api/table/{table_id}/view")
    )
    payload = _table_payload(table_id, view)

    def md(p: dict) -> str:
        head = "" if p.get("my_seat") is None else f"_Seated at seat {p['my_seat']}._"
        tail = ["Ready up with `duels_configure_table` action='ready'."]
        if p.get("timer_preset") not in (None, "none"):
            # Two games were lost here without a single bad play: the clock
            # runs on real time, so anything that pauses between tool calls
            # spends it. One call per turn is the defence.
            tail.insert(
                0,
                f"**This table is on a {p['timer_preset']} clock.** A turn is spent "
                "in real time whether or not anyone is thinking, and running out "
                "eliminates you outright - play each turn with `duels_play_turn` "
                "in a single call rather than one tool per move.",
            )
        return join_lines([head, "", _table_md(p), "", *tail])

    return render(payload, response_format, md)
