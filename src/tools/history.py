"""History tools: past matches, replays, leaderboard and seasons."""

from typing import Annotated, Optional

from fastmcp import Context
from pydantic import Field

from ..app import app_ctx, mcp
from ..formatting import as_json, bullet, empty_result, join_lines, render
from ..models import Limit, ResponseFmt, ResponseFormat
from ..toolkit import tool_errors


@mcp.tool(
    name="duels_get_match_history",
    annotations={
        "title": "Get Your Match History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_match_history(
    ctx: Context,
    limit: Limit = 20,
    cursor: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Continuation token from a previous call's next_cursor. This endpoint "
                "pages by cursor, not by offset."
            ),
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns your recent finished games - opponent, result, deck and date.

    Use for "how did my last games go" and to find a game_id to feed into
    duels_get_replay. Requires a session cookie.

    Pages by **cursor**, not offset: pass the previous response's next_cursor to
    continue. Do NOT use it for games still in progress (duels_list_active_games)
    or for aggregate win rates (duels_get_account_stats).

    Args:
        ctx (Context): Injected by FastMCP.
        limit (int): 1-100, default 20.
        cursor (Optional[str]): Continuation token from next_cursor.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"count": int, "games": [...], "next_cursor": str | null}. Game
        fields mirror what Duels.ink reports, typically including the game id,
        opponent, result and timestamp.
    Examples:
        - "How did my last games go?" -> call with defaults
        - "Show the next page" -> cursor from the previous response next_cursor

    """
    app = app_ctx(ctx)
    app.client.require_auth("Reading match history")
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = await app.client.get("/api/me/match-history", params=params)
    games = (data or {}).get("games") or []
    if not games:
        return empty_result("finished matches", "Play a ranked or table game first.")
    payload = {
        "count": len(games),
        "games": games,
        "next_cursor": (data or {}).get("next_cursor"),
    }

    def md(p: dict) -> str:
        lines = [f"# Match history ({p['count']})", ""]
        for g in p["games"]:
            result = g.get("result") or g.get("outcome") or "?"
            opponent = g.get("opponentName") or g.get("opponent") or "?"
            when = g.get("finishedAt") or g.get("createdAt") or ""
            lines.append(f"- **{result}** vs {opponent} - {when}\n  `{g.get('id') or g.get('gameId')}`")
        if p["next_cursor"]:
            lines += ["", f"_More available - pass `cursor='{p['next_cursor']}'`._"]
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_get_replay",
    annotations={
        "title": "Get a Game Replay",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_replay(
    ctx: Context,
    replay_id: Annotated[
        str,
        Field(
            description=(
                "Replay or game id, from duels_get_match_history or a finished game's "
                "replayId."
            ),
            min_length=8,
            max_length=64,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the recorded turn-by-turn replay of a finished game.

    Use to analyse a loss, review a line of play, or reconstruct what happened
    in a match you were not watching.

    Do NOT use for a game still in progress - duels_get_game_log has the live
    narration for those.

    Args:
        ctx (Context): Injected by FastMCP.
        replay_id (str): The replay or game id.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The replay document as Duels.ink stores it - metadata plus the
        ordered action/event stream. Shape is not fixed, so JSON mode is
        usually the more useful one here.

    Examples:
        - "Why did I lose that one?" -> replay_id from duels_get_match_history

    Error Handling:
        Returns "Error: Not found" when the id is wrong or the replay expired.
    """
    app = app_ctx(ctx)
    data = await app.client.get(f"/api/replay/{replay_id}")
    return render(
        data if isinstance(data, dict) else {"replay": data},
        response_format,
        lambda p: join_lines([f"# Replay {replay_id}", "", "```json", as_json(p)[:6000], "```"]),
    )


@mcp.tool(
    name="duels_get_leaderboard",
    annotations={
        "title": "Get the Ranked Leaderboard",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_leaderboard(
    ctx: Context,
    limit: Limit = 25,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the current ranked leaderboard, the active season, and your own rank when signed in.

    Use for "who is on top" and "where do I stand". The leaderboard itself is
    public; only currentUserRank needs a session cookie.

    Do NOT use for your personal win rate over time (duels_get_account_stats).

    Args:
        ctx (Context): Injected by FastMCP.
        limit (int): How many places to return (1-100, default 25).
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"season": {...}, "your_rank": {...} | null, "count": int,
        "leaderboard": [{"rank","name","rating", ...}]}.
    Examples:
        - "Who is number one this season?" -> call with defaults
        - "Where do I stand?" -> read your_rank

    """
    app = app_ctx(ctx)
    data = await app.client.get("/api/leaderboard", params={"limit": limit})
    rows = (data or {}).get("leaderboard") or []
    payload = {
        "season": (data or {}).get("seasonInfo"),
        "your_rank": (data or {}).get("currentUserRank"),
        "count": len(rows[:limit]),
        "leaderboard": rows[:limit],
    }

    def md(p: dict) -> str:
        lines = ["# Ranked leaderboard", ""]
        season = p.get("season") or {}
        if season:
            lines += [bullet("Season", season.get("name") or season.get("id")), ""]
        for i, row in enumerate(p["leaderboard"], start=1):
            rank = row.get("rank", i)
            name = row.get("name") or row.get("userName") or row.get("displayName") or "?"
            rating = row.get("rating") or row.get("points") or row.get("score") or ""
            lines.append(f"{rank}. **{name}** {rating}")
        if p.get("your_rank"):
            lines += ["", f"_Your rank: {p['your_rank']}_"]
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_get_seasons",
    annotations={
        "title": "Get Season Results",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_seasons(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns your per-season placements and the activity feed behind them.

    Use for "how did I do last season" and for a broader picture than the
    current-season leaderboard gives. Requires a session cookie.

    Do NOT use for a list of individual games (duels_get_match_history).

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"season_results": [...], "history_stats": {...}, "meta": {...}}
        as reported by Duels.ink.
    Examples:
        - "How did I place last season?" -> call with defaults

    """
    app = app_ctx(ctx)
    app.client.require_auth("Reading season results")
    seasons = await app.client.get("/api/account/season-results")
    history = await app.client.get("/api/account/history")
    payload = {
        "season_results": seasons,
        "history_stats": (history or {}).get("stats"),
        "meta": (history or {}).get("meta"),
    }
    return render(
        payload,
        response_format,
        lambda p: join_lines(
            [
                "# Seasons",
                "",
                "```json",
                as_json(p["season_results"])[:3000],
                "```",
                "## Aggregate stats",
                "```json",
                as_json(p["history_stats"])[:2000],
                "```",
            ]
        ),
    )
