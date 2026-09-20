"""In-game tools: read the board, make moves, answer prompts.

Every tool here talks to a live WebSocket held open for the duration of the
game (see gamews.py). The server decides what is legal, so these tools mostly
forward a choice and report the resulting state.
"""

from typing import Annotated, Any, Optional

from fastmcp import Context
from pydantic import Field

from ..app import AppContext, app_ctx, mcp
from ..client import DuelsError
from ..formatting import as_json, join_lines, render
from ..gamews import GameConnection
from ..models import CardInstanceId, GameId, Limit, ResponseFmt, ResponseFormat, SkipTriggers
from ..render import game_state_markdown, render_game_state
from ..toolkit import progress, tool_errors

# Response `type` to use for each prompt `type`. Triggers are the exception:
# they resolve or skip rather than echoing their own name.
PROMPT_RESPONSE_TYPES = {
    "boolean": "boolean",
    "select_target": "select_target",
    "select_card": "select_card",
    "select_numeric": "select_numeric",
}


async def _conn(app: AppContext, game_id: str) -> GameConnection:
    return await app.games.get(game_id, app.games.session_for(game_id))


async def _state_reply(
    app: AppContext,
    conn: GameConnection,
    response_format: ResponseFormat,
    headline: str = "",
) -> str:
    """Render the post-action state so the agent immediately sees the result."""
    game = await conn.refresh()
    payload = await render_game_state(game, app.catalog)
    if headline:
        payload["last_action"] = headline

    def md(p: dict) -> str:
        body = game_state_markdown(p)
        return f"{headline}\n\n{body}" if headline else body

    return render(payload, response_format, md)


def _prompt_options(prompt: dict) -> str:
    """Summarise the fields of a prompt that carry its selectable options.

    Prompt payloads vary by type, so rather than hardcode one field name we
    show the caller whichever option-bearing fields this prompt actually has.
    """
    interesting = (
        "validTargets",
        "validCards",
        "cards",
        "options",
        "choices",
        "triggers",
        "minSelect",
        "maxSelect",
        "min",
        "max",
        "yesLabel",
        "noLabel",
        "recommendedChoice",
    )
    found = {k: prompt[k] for k in interesting if k in prompt}
    if not found:
        found = {k: v for k, v in prompt.items() if k not in ("id", "player", "type", "required")}
    return f"The prompt carries: {found}"


def _resolve_prompt(game: dict, prompt_id: Optional[str]) -> dict:
    prompts = game.get("pendingPrompts") or []
    if not prompts:
        raise DuelsError(
            "There is no prompt waiting in this game. Call duels_get_game_state to see "
            "what the position actually needs."
        )
    if prompt_id:
        for p in prompts:
            if p.get("id") == prompt_id:
                return p
        raise DuelsError(
            f"No pending prompt with id '{prompt_id}'. Pending ids: "
            f"{', '.join(str(p.get('id')) for p in prompts)}."
        )
    return prompts[0]


# =================================================================
# Reading the game
# =================================================================
@mcp.tool(
    name="duels_get_game_state",
    annotations={
        "title": "Get the Current Game State",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_game_state(
    ctx: Context,
    game_id: GameId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the board, both hands' visible information, and the exact list of moves that are legal right now.

    This is the tool to call before every decision. Card ids are resolved to
    real names and stats, and each hand card says whether it can be played -
    and if not, why ("Need 3 ink"). The `legal_moves` list names the tool and
    arguments for each option, so you never have to know Lorcana's rules or
    guess what is allowed.

    It is phase-aware: during the coin toss it offers the play/draw choice,
    during the mulligan it offers the mulligan, and when it is the opponent's
    turn it tells you to wait rather than listing moves you cannot make.

    Do NOT use duels_get_legal_moves instead unless you only want the move list
    without the board. Do NOT call this in a tight loop while waiting for the
    opponent - use duels_wait_for_my_turn, which blocks efficiently.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: In JSON mode:
        {
            "game_id": str, "status": str,          # coin_toss | mulligan | playing | finished
            "turn_number": int, "my_turn": bool,
            "player_number": int, "state_version": int,
            "me": {
                "lore": int, "ink_available": int, "ink_total": int,
                "hand_count": int, "deck_count": int, "discard_count": int,
                "hand": [{"instance_id","card","cost","type","can":[...],"blocked":str}],
                "field": [...], "items": [...]
            },
            "opponent": { ... same, but hand_count only - their hand is hidden },
            "legal_moves": [{"tool": str, "why": str, "args": {...}}],
            "pending_prompts": [...],   # when a decision is waiting
            "winner": int, "i_won": bool    # once the game is over
        }

    Examples:
        - "What's the board?" -> call with the game_id
        - "What can I do?" -> read legal_moves
        - "Why can't I play this card?" -> read the card's "blocked" field
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    return await _state_reply(app, conn, response_format)


@mcp.tool(
    name="duels_get_legal_moves",
    annotations={
        "title": "List Legal Moves",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_legal_moves(
    ctx: Context,
    game_id: GameId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns just the legal moves for the current position, without the full board.

    Use when you already know the board and only need to re-check what is
    allowed - for example after an action changed the available ink.

    Do NOT use it as your main read: duels_get_game_state returns this same
    list plus the board, for roughly the same cost.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"game_id": str, "status": str, "my_turn": bool,
        "legal_moves": [{"tool": str, "why": str, "args": {...}}]}.
        An empty list means the game is over or you are not to act.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    game = await conn.refresh()
    full = await render_game_state(game, app.catalog)
    payload = {
        "game_id": full["game_id"],
        "status": full["status"],
        "my_turn": full["my_turn"],
        "legal_moves": full["legal_moves"],
    }
    if full.get("pending_prompts"):
        payload["pending_prompts"] = full["pending_prompts"]

    def md(p: dict) -> str:
        if not p["legal_moves"]:
            return "No legal moves: it is not your turn, or the game is over."
        lines = [f"# Legal moves ({p['status']})", ""]
        for m in p["legal_moves"]:
            lines.append(f"- {m['why']}\n  `{m['tool']}` with `{m['args']}`")
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_get_game_log",
    annotations={
        "title": "Get the Game Log",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_game_log(
    ctx: Context,
    game_id: GameId,
    limit: Limit = 40,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the narrated play-by-play of a game - what each side drew, played, quested and banished.

    Use to catch up on what happened while you were not looking, especially
    after duels_wait_for_my_turn returns and you want to know what the opponent
    did on their turn.

    Do NOT use it to decide a move - it is history, not state. Use
    duels_get_game_state for that. The log only covers the current connection;
    for a finished game use duels_get_replay.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        limit (int): How many of the most recent entries to return (1-100, default 40).
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"game_id": str, "count": int, "entries": [{"turn": int,
        "player": int, "type": str, "message": str}]}. Card placeholders in
        each message are already substituted with real card names.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    await conn.refresh()

    entries = []
    for entry in conn.logs[-limit:]:
        message = str(entry.get("message") or "")
        # Messages embed {card:N} placeholders indexing into cardRefs.
        for idx, ref in enumerate(entry.get("cardRefs") or []):
            message = message.replace(f"{{card:{idx}}}", str(ref.get("name") or ref.get("id")))
        entries.append(
            {
                "turn": entry.get("turnNumber"),
                "player": entry.get("player"),
                "type": entry.get("type"),
                "message": message,
            }
        )

    payload = {"game_id": game_id, "count": len(entries), "entries": entries}

    def md(p: dict) -> str:
        if not p["entries"]:
            return "No log entries yet for this game."
        lines = [f"# Game log ({p['count']} entries)", ""]
        turn = None
        for e in p["entries"]:
            if e["turn"] != turn:
                turn = e["turn"]
                lines.append(f"\n**Turn {turn}**")
            who = {1: "P1", 2: "P2"}.get(e["player"], "-")
            lines.append(f"- [{who}] {e['message']}")
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_wait_for_my_turn",
    annotations={
        "title": "Wait Until It Is Your Turn",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_wait_for_my_turn(
    ctx: Context,
    game_id: GameId,
    timeout_seconds: Annotated[
        int,
        Field(
            default=60,
            ge=5,
            le=300,
            description="How long to wait before giving up and returning the current state.",
        ),
    ] = 60,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Blocks until it is your turn (or a prompt needs you), then returns the new game state.

    Use instead of polling duels_get_game_state in a loop. It waits on the live
    socket, so it returns the moment the opponent finishes rather than on a
    fixed interval.

    It also returns early if the game ends or a prompt appears for you. If the
    timeout expires it returns the current state anyway with timed_out set -
    that is not an error, just a slow opponent; call it again to keep waiting.

    Do NOT use it when it is already your turn - it returns immediately and
    duels_get_game_state is the clearer call. Do NOT wrap it in a retry loop
    with a short timeout; give it a long one instead.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        timeout_seconds (int): 5-300, default 60.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The same structure as duels_get_game_state, plus "timed_out": bool.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)

    def ready(game: Optional[dict]) -> bool:
        """True once there is actually something for this player to do.

        Being *in* the coin toss or mulligan phase is not enough - if the choice
        belongs to the opponent we must keep waiting, otherwise this returns
        instantly and the caller spins.
        """
        if not game:
            return False
        if game.get("winner") is not None:
            return True
        if game.get("pendingPrompts"):
            return True

        status = game.get("status")
        if status == "coin_toss":
            return bool((game.get("coinToss") or {}).get("isYourChoice"))
        if status == "mulligan":
            mulligan = game.get("mulliganState") or {}
            return bool(mulligan.get("canSubmit")) and not mulligan.get("myDone")

        return game.get("viewingAs") == game.get("currentPlayer")

    await progress(ctx, 0.1, "Waiting for the opponent...")
    satisfied = await conn.wait_for_update(timeout=float(timeout_seconds), predicate=ready)
    await progress(ctx, 1.0, "Done")

    game = await conn.refresh()
    payload = await render_game_state(game, app.catalog)
    payload["timed_out"] = not satisfied

    def md(p: dict) -> str:
        head = (
            f"_Still not your turn after {timeout_seconds}s - call again to keep waiting._\n\n"
            if p["timed_out"]
            else ""
        )
        return head + game_state_markdown(p)

    return render(payload, response_format, md)


# =================================================================
# Making moves
# =================================================================
@mcp.tool(
    name="duels_ink_card",
    annotations={
        "title": "Put a Card into the Inkwell",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_ink_card(
    ctx: Context,
    game_id: GameId,
    card_instance_id: CardInstanceId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Puts one card from your hand into your inkwell, increasing the ink you can spend each turn.

    You may ink at most once per turn, and only cards whose `inkable` is true.
    duels_get_game_state marks eligible cards with "canInk"; the state's
    `ink_actions_left` tells you whether you have already inked this turn.

    Do NOT use this to play a card onto the board - that is duels_play_card.
    Inking is permanent for the game: the card is spent as a resource.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        card_instance_id (str): The hand card's instanceId, not its definitionId.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state, as duels_get_game_state returns it.

    Error Handling:
        Returns "Error: Duels.ink rejected ADD_TO_INK: ..." when the card is not
        inkable or the turn's ink has already been used.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    await conn.send_action({"type": "ADD_TO_INK", "cardInstanceId": card_instance_id})
    return await _state_reply(app, conn, response_format, "Inked a card.")


@mcp.tool(
    name="duels_play_card",
    annotations={
        "title": "Play a Card",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_play_card(
    ctx: Context,
    game_id: GameId,
    card_instance_id: CardInstanceId,
    singer_instance_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "For songs only: instanceIds of the characters singing it, which pays the "
                "cost by exerting them instead of spending ink. duels_get_game_state lists "
                "each song's valid_singers."
            ),
            max_length=4,
        ),
    ] = None,
    shift_target_instance_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For Shift cards: the instanceId of your character on the board to shift "
                "on top of, paying the cheaper Shift cost."
            ),
        ),
    ] = None,
    discarded_card_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Hand instanceIds to discard when the card's cost requires it "
                "(for example 'Shift: Discard 2 cards')."
            ),
            max_length=10,
        ),
    ] = None,
    skip_optional_triggers: SkipTriggers = False,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Plays a card from your hand - a character, action, item, location, or a song (optionally sung).

    Check duels_get_game_state first: each hand card reports "canPlay", or a
    "blocked" reason such as "Need 3 ink". Playing a card often creates a
    prompt (a "you may..." ability); when it does, the returned state shows it
    and you answer with duels_respond_to_prompt.

    Do NOT use this to put a card into the inkwell (duels_ink_card) or to quest
    with a character already on the board (duels_quest).

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        card_instance_id (str): The hand card's instanceId.
        singer_instance_ids (Optional[list[str]]): Characters singing a song.
        shift_target_instance_id (Optional[str]): Board character to Shift onto.
        discarded_card_ids (Optional[list[str]]): Cards discarded to pay a cost.
        skip_optional_triggers (bool): Decline optional "you may" triggers.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state. If a decision is now required, it appears
        under pending_prompts and legal_moves points at duels_respond_to_prompt.

    Examples:
        - Play a character: card_instance_id only
        - Sing a song for free: card_instance_id + singer_instance_ids=['<char>']
        - Shift onto a character: card_instance_id + shift_target_instance_id
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)

    action: dict[str, Any] = {"type": "PLAY_CARD", "cardInstanceId": card_instance_id}
    if singer_instance_ids:
        action["singerInstanceIds"] = singer_instance_ids
    if shift_target_instance_id:
        action["shiftTargetInstanceId"] = shift_target_instance_id
    if discarded_card_ids:
        action["discardedCardIds"] = discarded_card_ids
    if skip_optional_triggers:
        action["skipNonMandatoryTriggers"] = True

    await conn.send_action(action)
    return await _state_reply(app, conn, response_format, "Played a card.")


@mcp.tool(
    name="duels_quest",
    annotations={
        "title": "Quest for Lore",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_quest(
    ctx: Context,
    game_id: GameId,
    card_instance_id: CardInstanceId,
    skip_optional_triggers: SkipTriggers = False,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Quests with one of your characters: exerts it and gains you its lore value.

    Questing is how you win - first to 20 lore takes the game. A character can
    only quest if it is ready (not exerted) and its ink is dry (it was not
    played this turn). duels_get_game_state marks eligible characters "canQuest".

    Do NOT use this to attack an opposing character - that is duels_challenge.
    An exerted character cannot be used again this turn, and exerted characters
    can be challenged by the opponent.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        card_instance_id (str): Your board character's instanceId.
        skip_optional_triggers (bool): Decline optional "you may" triggers.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state, with your new lore total.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    action: dict[str, Any] = {"type": "QUEST", "cardInstanceId": card_instance_id}
    if skip_optional_triggers:
        action["skipNonMandatoryTriggers"] = True
    await conn.send_action(action)
    return await _state_reply(app, conn, response_format, "Quested for lore.")


@mcp.tool(
    name="duels_challenge",
    annotations={
        "title": "Challenge an Opposing Character",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_challenge(
    ctx: Context,
    game_id: GameId,
    attacker_instance_id: Annotated[
        str,
        Field(
            description="Your character's instanceId. It must be ready and its ink dry.",
            min_length=8,
            max_length=64,
        ),
    ],
    target_instance_id: Annotated[
        str,
        Field(
            description=(
                "The opposing character's instanceId. Normally it must be exerted, "
                "unless something makes it challengeable while ready."
            ),
            min_length=8,
            max_length=64,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Challenges an opposing character: both deal damage equal to their strength to each other.

    Your attacker exerts. Either character is banished if its accumulated damage
    reaches its willpower. duels_get_game_state marks your eligible attackers
    "canChallenge" and shows each opposing character's damage and willpower, so
    you can work out which trades are favourable before committing.

    Do NOT use this to gain lore (that is duels_quest). Note that Evasive
    characters can only be challenged by other Evasive characters, and Bodyguard
    characters must be challenged first - the server enforces both and will
    reject an illegal challenge.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        attacker_instance_id (str): Your character's instanceId.
        target_instance_id (str): The opposing character's instanceId.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state, showing the damage dealt and anything banished.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    await conn.send_action(
        {
            "type": "ATTACK",
            "attackerInstanceId": attacker_instance_id,
            "targetInstanceId": target_instance_id,
        }
    )
    return await _state_reply(app, conn, response_format, "Challenged.")


@mcp.tool(
    name="duels_end_turn",
    annotations={
        "title": "End Your Turn",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_end_turn(
    ctx: Context,
    game_id: GameId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Ends your turn and passes to the opponent.

    Before ending, check duels_get_game_state for anything still worth doing:
    unspent ink, characters that can still quest or challenge, and your once-per-turn
    ink drop. None of it carries over.

    The current turn number and player are sent with the request so a stale call
    cannot accidentally end a later turn - if the game has moved on, Duels.ink
    rejects it rather than doing the wrong thing.

    Do NOT use this to leave or lose a game - that is duels_concede. Ending a
    turn is a normal move and hands play to the opponent.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state. Follow with duels_wait_for_my_turn to block
        until the opponent has finished.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    game = await conn.refresh()
    await conn.send_action(
        {
            "type": "END_TURN",
            "expectedTurnNumber": game.get("turnNumber"),
            "expectedCurrentPlayer": game.get("currentPlayer"),
        }
    )
    return await _state_reply(app, conn, response_format, "Turn ended.")


# =================================================================
# Prompts
# =================================================================
@mcp.tool(
    name="duels_respond_to_prompt",
    annotations={
        "title": "Answer a Pending Game Prompt",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_respond_to_prompt(
    ctx: Context,
    game_id: GameId,
    choice: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For a 'boolean' prompt: 'yes' or 'no'. For a 'select_trigger' prompt: "
                "'resolve' to use the ability or 'skip' to decline it. Ignored for the "
                "selection prompts, which use target_instance_ids / selected_card_ids."
            ),
        ),
    ] = None,
    target_instance_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "For a 'select_target' prompt: the chosen instanceIds. The prompt's "
                "validTargets lists what is allowed, and minSelect/maxSelect how many."
            ),
            max_length=20,
        ),
    ] = None,
    selected_card_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description="For a 'select_card' prompt: the chosen card instanceIds.",
            max_length=20,
        ),
    ] = None,
    numeric_value: Annotated[
        Optional[int],
        Field(default=None, description="For a 'select_numeric' prompt: the chosen number."),
    ] = None,
    trigger_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "For a 'select_trigger' prompt with more than one trigger: which one to "
                "act on. Defaults to the first pending trigger."
            ),
        ),
    ] = None,
    prompt_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Which prompt to answer. Defaults to the first pending one.",
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Answers the decision a card ability is waiting on, so the game can continue.

    Playing cards often triggers "you may..." abilities and targeting choices.
    While a prompt is pending nothing else can happen, so answer it first.
    Read the prompt from duels_get_game_state's pending_prompts: it carries the
    ability's name and description, and for targeting prompts the exact list of
    validTargets.

    Which argument to use depends on the prompt's `type`:
      - boolean         -> choice='yes' or 'no' (the prompt's yesLabel/noLabel
                           describe what each means, and recommendedChoice hints)
      - select_trigger  -> choice='resolve' or 'skip' (+ trigger_id if several)
      - select_target   -> target_instance_ids=[...]
      - select_card     -> selected_card_ids=[...]
      - select_numeric  -> numeric_value=N

    Do NOT use duels_send_game_action for prompts - it takes a raw payload and
    is easy to get wrong; this tool builds the correct one for you.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        choice (Optional[str]): yes | no | resolve | skip.
        target_instance_ids (Optional[list[str]]): For select_target.
        selected_card_ids (Optional[list[str]]): For select_card.
        numeric_value (Optional[int]): For select_numeric.
        trigger_id (Optional[str]): For select_trigger with several triggers.
        prompt_id (Optional[str]): Defaults to the first pending prompt.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state. Answering one prompt often reveals the
        next one - check pending_prompts again.

    Error Handling:
        Returns a message naming the prompt's type and the argument it needs
        when the wrong one is supplied.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    game = await conn.refresh()
    prompt = _resolve_prompt(game, prompt_id)

    ptype = prompt.get("type")
    pid = prompt.get("id")
    response: dict[str, Any] = {"promptId": pid}
    normalised = (choice or "").strip().lower()

    if ptype == "select_trigger":
        triggers = prompt.get("triggers") or []
        chosen = trigger_id or (triggers[0].get("id") if triggers else None)
        if not chosen:
            raise DuelsError("This trigger prompt lists no triggers to act on.")
        if normalised in ("skip", "no", "decline"):
            response.update({"type": "skip_trigger", "triggerId": chosen})
        elif normalised in ("resolve", "yes", "accept", ""):
            response.update({"type": "resolve_trigger", "triggerId": chosen})
        else:
            raise DuelsError(
                f"choice must be 'resolve' or 'skip' for a select_trigger prompt, got {choice!r}."
            )

    elif ptype == "boolean":
        if normalised not in ("yes", "no", "true", "false"):
            raise DuelsError(
                "This is a boolean prompt - pass choice='yes' or choice='no'. "
                f"yes means {prompt.get('yesLabel')!r}, no means {prompt.get('noLabel')!r}."
            )
        response.update({"type": "boolean", "value": normalised in ("yes", "true")})

    elif ptype == "select_target":
        if not target_instance_ids:
            raise DuelsError(
                "This is a select_target prompt - pass target_instance_ids. "
                + _prompt_options(prompt)
            )
        response.update({"type": "select_target", "targetInstanceIds": target_instance_ids})

    elif ptype == "select_card":
        if not selected_card_ids:
            raise DuelsError(
                "This is a select_card prompt - pass selected_card_ids. "
                + _prompt_options(prompt)
            )
        response.update({"type": "select_card", "selectedCardIds": selected_card_ids})

    elif ptype == "select_numeric":
        if numeric_value is None:
            raise DuelsError(
                "This is a select_numeric prompt - pass numeric_value. " + _prompt_options(prompt)
            )
        response.update({"type": "select_numeric", "numericValue": numeric_value})

    else:
        # Unknown prompt type: pass through whatever was supplied rather than
        # blocking the game on a shape we have not seen before.
        response["type"] = PROMPT_RESPONSE_TYPES.get(ptype, ptype)
        if target_instance_ids:
            response["targetInstanceIds"] = target_instance_ids
        if selected_card_ids:
            response["selectedCardIds"] = selected_card_ids
        if numeric_value is not None:
            response["numericValue"] = numeric_value
        if normalised in ("yes", "no"):
            response["value"] = normalised == "yes"

    await conn.send_action({"type": "RESPOND_TO_PROMPT", "response": response})
    return await _state_reply(app, conn, response_format, f"Answered the {ptype} prompt.")


# =================================================================
# Escape hatch and leaving a game
# =================================================================
@mcp.tool(
    name="duels_send_game_action",
    annotations={
        "title": "Send a Raw Game Action",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_send_game_action(
    ctx: Context,
    game_id: GameId,
    action_type: Annotated[
        str,
        Field(
            description=(
                "The action type, e.g. 'CHOOSE_STARTING_PLAYER', 'MULLIGAN', "
                "'ACTIVATE_ABILITY', 'MOVE_TO_LOCATION', 'BOOST', 'REQUEST_UNDO', "
                "'ABANDON_BOT_GAME'."
            ),
            min_length=3,
            max_length=48,
        ),
    ],
    payload: Annotated[
        Optional[dict],
        Field(
            default=None,
            description=(
                "The action's remaining fields, merged with 'type'. Examples: "
                "{'choice': 'play'} for CHOOSE_STARTING_PLAYER, "
                "{'selectedCardIds': []} for MULLIGAN, "
                "{'cardInstanceId': '<uuid>'} for ACTIVATE_ABILITY."
            ),
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Sends any game action directly, for the cases the dedicated tools do not cover.

    Duels.ink accepts about 50 action types. The common ones have their own
    tools (duels_ink_card, duels_play_card, duels_quest, duels_challenge,
    duels_end_turn, duels_respond_to_prompt) and you should prefer those - they
    validate arguments and build the payload for you.

    Use this one for the long tail. Verified payloads:
      - CHOOSE_STARTING_PLAYER: {'choice': 'play'} or {'choice': 'draw'}
      - MULLIGAN: {'selectedCardIds': [...]} - an empty list keeps the hand
      - MOVE_TO_LOCATION: {'characterInstanceId': ..., 'locationInstanceId': ...}
        (note it is characterInstanceId here, NOT cardInstanceId)
      - ACTIVATE_ABILITY: {'cardInstanceId': ..., 'abilityName': ...}
      - BOOST: {'cardInstanceId': ...}
      - REQUEST_UNDO, ABANDON_BOT_GAME: no extra fields
    duels_get_game_state's legal_moves names the exact payload when one of these
    applies.

    Do NOT use it for prompts (duels_respond_to_prompt builds the nested payload
    correctly) or for the common moves above - a malformed payload here is
    rejected by the server, and some action types end the game.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        action_type (str): The action type constant.
        payload (Optional[dict]): The action's other fields.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The updated game state, or an error naming what Duels.ink rejected.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    action = {"type": action_type.strip().upper(), **(payload or {})}
    await conn.send_action(action)
    if action["type"] in ("ABANDON_BOT_GAME", "CONCEDE"):
        result = await _state_reply(app, conn, response_format, f"Sent {action['type']}.")
        await app.games.drop(game_id)
        return result
    return await _state_reply(app, conn, response_format, f"Sent {action['type']}.")


@mcp.tool(
    name="duels_concede",
    annotations={
        "title": "Concede or Abandon a Game",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_concede(
    ctx: Context,
    game_id: GameId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Ends a game immediately by conceding it - you lose, and the game is over.

    This cannot be undone. For a bot game it sends ABANDON_BOT_GAME, which also
    clears the "one bot game at a time" lock so you can start another; against a
    human it concedes the match and counts as a loss.

    Use it to clean up an abandoned practice game, or when a position is
    hopeless. Do NOT use it to step away temporarily - the game stays available
    and duels_list_active_games will find it again.

    Args:
        ctx (Context): Injected by FastMCP.
        game_id (str): Game UUID.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: A confirmation, plus the final state.
    """
    app = app_ctx(ctx)
    conn = await _conn(app, game_id)
    game = await conn.refresh()

    # Already over: report the result and release the socket instead of sending
    # an action the server will reject.
    if game.get("winner") is not None or game.get("status") in ("finished", "complete", "ended"):
        result = await _state_reply(
            app, conn, response_format, "This game is already over - nothing to concede."
        )
        await app.games.drop(game_id)
        return result

    action = "ABANDON_BOT_GAME" if game.get("isBotGame") else "CONCEDE"
    await conn.send_action({"type": action})
    result = await _state_reply(app, conn, response_format, f"Game ended ({action}).")
    await app.games.drop(game_id)
    return result
