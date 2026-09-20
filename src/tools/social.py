"""Social tools: friends, presence and pending invites."""

from typing import Annotated

from fastmcp import Context
from pydantic import Field

from ..app import app_ctx, mcp
from ..formatting import as_json, empty_result, join_lines, render
from ..models import ResponseFmt, ResponseFormat
from ..toolkit import tool_errors


@mcp.tool(
    name="duels_list_friends",
    annotations={
        "title": "List Friends and Who Is Online",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_list_friends(
    ctx: Context,
    online_only: Annotated[
        bool,
        Field(default=False, description="Return only friends currently online."),
    ] = False,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns your friends list with online presence, plus incoming and outgoing friend requests.

    Use to see who is around before creating a table to play with someone.
    Requires a session cookie.

    Do NOT use for game invitations - those are in duels_list_pending_invites.

    Examples:
    - "Who is online?" -> online_only=True
    - "Show my friends list" -> call with defaults

    Args:
        ctx (Context): Injected by FastMCP.
        online_only (bool): Filter to friends currently online. Default False.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"count": int, "friends": [{"id","name","online","last_seen"}],
        "incoming_requests": [...], "outgoing_requests": [...]}.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Listing friends")

    data = await app.client.get("/api/friends")
    presence_data = await app.client.get("/api/friends/presence")
    presence = (presence_data or {}).get("presence") or {}
    last_seen = (presence_data or {}).get("lastSeen") or {}

    friends = []
    for f in (data or {}).get("friends") or []:
        fid = f.get("id") or f.get("userId")
        is_online = bool(presence.get(fid)) if isinstance(presence, dict) else False
        if online_only and not is_online:
            continue
        friends.append(
            {
                "id": fid,
                "name": f.get("name") or f.get("userName") or f.get("displayName"),
                "online": is_online,
                "last_seen": last_seen.get(fid) if isinstance(last_seen, dict) else None,
            }
        )

    if not friends and not online_only:
        return empty_result("friends", "Add one with duels_send_friend_request.")
    if not friends:
        return empty_result("friends online right now")

    payload = {
        "count": len(friends),
        "friends": friends,
        "incoming_requests": (data or {}).get("incoming") or [],
        "outgoing_requests": (data or {}).get("outgoing") or [],
    }

    def md(p: dict) -> str:
        lines = [f"# Friends ({p['count']})", ""]
        for f in p["friends"]:
            dot = "online" if f["online"] else "offline"
            lines.append(f"- **{f['name']}** - {dot}  `{f['id']}`")
        if p["incoming_requests"]:
            lines += ["", f"## Incoming requests ({len(p['incoming_requests'])})", "```json",
                      as_json(p["incoming_requests"])[:1500], "```"]
        if p["outgoing_requests"]:
            lines += ["", f"## Outgoing requests ({len(p['outgoing_requests'])})"]
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_send_friend_request",
    annotations={
        "title": "Send a Friend Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_send_friend_request(
    ctx: Context,
    user_id: Annotated[
        str,
        Field(
            description=(
                "The Duels.ink user id to befriend. Leaderboard rows and match history "
                "entries carry user ids."
            ),
            min_length=3,
            max_length=64,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Sends a friend request to another Duels.ink player.

    This is visible to the other person, so confirm with the user who they want
    to add before calling it. Requires a session cookie.

    Do NOT use it to invite someone to a game - share the link from
    duels_create_table instead.

    Examples:
    - "Add that player as a friend" -> user_id from a leaderboard row or match history

    Args:
        ctx (Context): Injected by FastMCP.
        user_id (str): The recipient's Duels.ink user id.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: Whatever Duels.ink reports, typically {"success": bool}.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Sending a friend request")
    data = await app.client.post("/api/friends/request", {"toUserId": user_id})
    return render(
        data if isinstance(data, dict) else {"result": data},
        response_format,
        lambda p: f"Friend request sent to `{user_id}`.",
    )


@mcp.tool(
    name="duels_list_pending_invites",
    annotations={
        "title": "List Pending Game Invites",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_list_pending_invites(
    ctx: Context,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns game invitations waiting for you, and the ones you have sent.

    Use to find a table someone invited you to. Join it with duels_join_table
    using the table id from the invite. Requires a session cookie.

    Do NOT use for friend requests - those are in duels_list_friends.

    Examples:
    - "Did anyone invite me to a game?" -> call with defaults

    Args:
        ctx (Context): Injected by FastMCP.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"incoming": [...], "outgoing": [...]} as reported by Duels.ink.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Listing invites")
    data = await app.client.get("/api/me/pending-invites")
    payload = {
        "incoming": (data or {}).get("invites") or [],
        "outgoing": (data or {}).get("outgoing") or [],
    }
    if not payload["incoming"] and not payload["outgoing"]:
        return empty_result("pending invites")

    def md(p: dict) -> str:
        lines = ["# Pending invites", ""]
        if p["incoming"]:
            lines += [f"## Incoming ({len(p['incoming'])})", "```json", as_json(p["incoming"])[:2000], "```"]
        if p["outgoing"]:
            lines += [f"## Outgoing ({len(p['outgoing'])})", "```json", as_json(p["outgoing"])[:1500], "```"]
        return join_lines(lines)

    return render(payload, response_format, md)
