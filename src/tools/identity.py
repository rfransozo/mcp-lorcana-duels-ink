"""Identity and account tools."""

from fastmcp import Context

from ..app import app_ctx, mcp
from ..formatting import as_json, bullet, join_lines, render
from ..models import ResponseFmt, ResponseFormat
from ..toolkit import tool_errors


@mcp.tool(
    name="duels_whoami",
    annotations={
        "title": "Show Duels.ink Account and Connection Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_whoami(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the signed-in Duels.ink account, whether a session cookie is configured, and the current site build id.

    Use this first whenever something unexpected happens: it distinguishes
    "no cookie configured" from "cookie expired" from "Duels.ink changed", and
    reports the build id that other error messages refer to.

    Do NOT use it for gameplay statistics - duels_get_account_stats has win
    rates and season results.

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: In JSON mode:
        {
            "authenticated": bool,      # a valid session was found
            "cookie_configured": bool,  # DUELS_SESSION_COOKIE was set
            "user": {                   # null when anonymous
                "id": str, "name": str, "email": str,
                "role": str, "features": list[str]
            },
            "session_expires_at": str | null,   # ISO 8601
            "build_id": str | null,             # current Duels.ink build
            "active_game_connections": list[str],
            "base_url": str
        }

    Examples:
        - "Am I logged in to Duels.ink?" -> call with defaults
        - "Why do my deck tools fail?" -> authenticated=false means the cookie
          is missing or expired
        - "What build is Duels.ink on?" -> read build_id
    """
    app = app_ctx(ctx)
    session = await app.client.get_session() if app.client.authenticated else None
    build_id = await app.client.build_id()

    user = (session or {}).get("user") or {}
    payload = {
        "authenticated": bool(session),
        "cookie_configured": app.client.authenticated,
        "user": {
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "features": user.get("features") or [],
        }
        if session
        else None,
        "session_expires_at": ((session or {}).get("session") or {}).get("expiresAt"),
        "build_id": build_id,
        "active_game_connections": app.games.active_ids(),
        "base_url": app.client.base_url,
    }

    def md(p: dict) -> str:
        if not p["cookie_configured"]:
            status = (
                "**Not configured.** Anonymous mode: card search, public decks and bot "
                "games work; your decks, ranked play, tables, friends and history do not.\n\n"
                "Set `DUELS_SESSION_COOKIE` to sign in."
            )
        elif not p["authenticated"]:
            status = (
                "**Cookie configured but rejected.** It has most likely expired - grab a "
                "fresh one from DevTools > Application > Cookies > https://duels.ink "
                "(`__Secure-better-auth.session_token`)."
            )
        else:
            u = p["user"]
            status = f"Signed in as **{u.get('name')}** ({u.get('email')})"
        return join_lines(
            [
                "# Duels.ink connection",
                "",
                status,
                "",
                bullet("Role", (p["user"] or {}).get("role")),
                bullet("Features", (p["user"] or {}).get("features")),
                bullet("Session expires", p["session_expires_at"]),
                bullet("Build id", p["build_id"]),
                bullet("Active game connections", p["active_game_connections"] or "none"),
            ]
        )

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_get_account_stats",
    annotations={
        "title": "Get Personal Duels.ink Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_account_stats(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the signed-in player's profile, lifetime statistics and per-season results.

    Use for "how am I doing" questions - win rate, rank, placement, season
    history. Requires a configured session cookie.

    Do NOT use for a list of individual matches (duels_get_match_history) or
    for global rankings across all players (duels_get_leaderboard).

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"profile": {...}, "stats": {...}, "seasons": [...]}. Field names
        mirror the Duels.ink account endpoints rather than a fixed schema. Any
        section that fails individually comes back as
        {"unavailable": "<reason>"} so a partial answer is still returned.

    Examples:
        - "What's my win rate?" -> read stats
        - "How did I place last season?" -> read seasons

    Error Handling:
        Returns "Error: ... requires a signed-in Duels.ink account" when no
        cookie is configured.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Reading account statistics")

    payload: dict = {}
    for key, path in (
        ("profile", "/api/account/profile"),
        ("stats", "/api/account/personal-stats"),
        ("seasons", "/api/account/season-results"),
    ):
        try:
            payload[key] = await app.client.get(path)
        except Exception as exc:  # noqa: BLE001 - partial data is still useful
            payload[key] = {"unavailable": str(exc)}

    def md(p: dict) -> str:
        return join_lines(
            [
                "# Your Duels.ink account",
                "",
                "## Profile",
                "```json",
                as_json(p.get("profile")),
                "```",
                "## Lifetime stats",
                "```json",
                as_json(p.get("stats")),
                "```",
                "## Seasons",
                "```json",
                as_json(p.get("seasons")),
                "```",
            ]
        )

    return render(payload, response_format, md)
