"""Deck tools: list, inspect, build, edit and browse community decks."""

import re
from collections import Counter
from typing import Annotated, Any, Optional

from fastmcp import Context
from pydantic import Field

from ..app import AppContext, app_ctx, mcp
from ..client import DuelsError
from ..formatting import bullet, empty_result, join_lines, paginate, pagination_footer, render
from ..models import DeckCardIds, DeckId, Limit, Offset, ResponseFmt, ResponseFormat
from ..toolkit import tool_errors

# "4 Flotsam - Slippery as an Eel" / "4x Flotsam" / "4 10-71"
DECKLIST_LINE = re.compile(r"^\s*(\d+)\s*x?\s+(.+?)\s*$")


def _deck_summary(deck: dict) -> dict:
    return {
        "id": deck.get("id"),
        "name": deck.get("name"),
        "card_count": deck.get("cardCount"),
        "colors": deck.get("colors") or [],
        "legal_formats": deck.get("legalFormats") or [],
        "valid": deck.get("valid"),
        "visibility": deck.get("visibility"),
        "owner": deck.get("ownerName"),
        "likes": deck.get("likeCount"),
        "updated_at": deck.get("updatedAt"),
    }


def _deck_list_md(title: str, payload: dict) -> str:
    lines = [f"# {title}", ""]
    for d in payload["decks"]:
        flags = []
        if d.get("colors"):
            flags.append("/".join(str(c).title() for c in d["colors"]))
        if d.get("card_count") is not None:
            flags.append(f"{d['card_count']} cards")
        if d.get("valid") is False:
            flags.append("not tournament-legal")
        if d.get("owner"):
            flags.append(f"by {d['owner']}")
        if d.get("likes"):
            flags.append(f"{d['likes']} likes")
        lines.append(f"- **{d.get('name')}** ({', '.join(flags)})\n  `{d.get('id')}`")
    lines += ["", pagination_footer(payload)]
    return join_lines(lines)


async def _fetch_deck(app: AppContext, deck_id: str) -> dict:
    data = await app.client.get(f"/api/decks/{deck_id}")
    deck = (data or {}).get("deck") if isinstance(data, dict) else None
    return deck or data or {}


@mcp.tool(
    name="duels_list_my_decks",
    annotations={
        "title": "List Your Saved Decks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_list_my_decks(
    ctx: Context,
    limit: Limit = 50,
    offset: Offset = 0,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the decks saved on your Duels.ink account, with their ids, colours and legality.

    Use to find a deck id before starting a game or joining a queue. Requires a
    session cookie.

    Do NOT use for community decks (duels_browse_public_decks) or for a deck's
    full card list (duels_get_deck).

    Examples:
    - "Which decks do I have?" -> call with defaults
    - "Pick a deck for ranked" -> take an id from here into duels_join_matchmaking

    Args:
        ctx (Context): Injected by FastMCP.
        limit (int): 1-100, default 50.
        offset (int): Pagination offset.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"total","count","offset","has_more","next_offset",
        "decks": [{"id","name","card_count","colors","legal_formats","valid",
        "visibility","updated_at"}]}.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Listing your decks")
    data = await app.client.get("/api/decks")
    decks = [_deck_summary(d) for d in (data or {}).get("decks") or []]
    page = decks[offset : offset + limit]
    if not page:
        return empty_result("decks on your account", "Create one with duels_create_deck.")
    payload = paginate(page, len(decks), offset, key="decks")
    return render(payload, response_format, lambda p: _deck_list_md("Your decks", p))


@mcp.tool(
    name="duels_browse_public_decks",
    annotations={
        "title": "Browse Community Decks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_browse_public_decks(
    ctx: Context,
    query: Annotated[
        Optional[str],
        Field(default=None, description="Filter by deck name, case-insensitive.", max_length=80),
    ] = None,
    limit: Limit = 25,
    offset: Offset = 0,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns community and suggested decks shared publicly on Duels.ink - around a thousand of them.

    Works **without authentication**, which makes it the easiest way to get a
    playable decklist for duels_start_bot_game when no account is configured.

    Do NOT use for your own decks (duels_list_my_decks). Note that some listed
    decks are precons or curated libraries and are not tournament-legal - check
    the `valid` flag before queueing with one.

    Examples:
    - "Find a ruby deck to practise against" -> query='ruby'
    - "Give me any playable deck" -> call with defaults and take the first valid one

    Args:
        ctx (Context): Injected by FastMCP.
        query (Optional[str]): Substring match on the deck name.
        limit (int): 1-100, default 25.
        offset (int): Pagination offset.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The same envelope as duels_list_my_decks, with "owner" and "likes"
        populated.

    """
    app = app_ctx(ctx)
    data = await app.client.get("/api/decks/public", authed=False)
    decks = [_deck_summary(d) for d in (data or {}).get("decks") or []]
    if query:
        needle = query.lower()
        decks = [d for d in decks if needle in str(d.get("name", "")).lower()]
    page = decks[offset : offset + limit]
    if not page:
        return empty_result("public decks matching that filter", "Try a broader query.")
    payload = paginate(page, len(decks), offset, key="decks")
    return render(payload, response_format, lambda p: _deck_list_md("Community decks", p))


@mcp.tool(
    name="duels_get_deck",
    annotations={
        "title": "Get a Deck's Full Card List",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_deck(
    ctx: Context,
    deck_id: DeckId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns a deck's full contents with card names and quantities, plus its colours and legality.

    Use to inspect a deck before playing it, to copy a community list, or to
    check what you would be changing before duels_update_deck. Card ids are
    resolved to names for you.

    Do NOT use to search the card catalog (duels_search_cards).

    Examples:
    - "What is in my Tourmaline deck?" -> deck_id from duels_list_my_decks
    - "Copy this community list" -> pass its card_ids to duels_create_deck

    Args:
        ctx (Context): Injected by FastMCP.
        deck_id (str): Deck UUID, from duels_list_my_decks or duels_browse_public_decks.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"id","name","card_count","colors","legal_formats","valid","owner",
        "card_ids": [...],        # flat, one entry per copy - pass to duels_start_bot_game
        "entries": [{"definition_id","name","quantity","cost","type"}]}
    """
    app = app_ctx(ctx)
    deck = await _fetch_deck(app, deck_id)
    card_ids = deck.get("cardIds") or []
    resolved = await app.catalog.resolve_many(set(card_ids))
    counts = Counter(card_ids)

    entries = []
    for definition_id, quantity in counts.most_common():
        card = resolved.get(definition_id) or {}
        entries.append(
            {
                "definition_id": definition_id,
                "name": card.get("fullName") or definition_id,
                "quantity": quantity,
                "cost": card.get("cost"),
                "type": card.get("type"),
            }
        )
    entries.sort(key=lambda e: (e["cost"] if e["cost"] is not None else 99, e["name"]))

    payload = {**_deck_summary(deck), "card_ids": card_ids, "entries": entries}

    def md(p: dict) -> str:
        lines = [
            f"# {p.get('name')}",
            "",
            bullet("Cards", p.get("card_count")),
            bullet("Colors", [str(c).title() for c in p.get("colors") or []]),
            bullet("Legal formats", p.get("legal_formats")),
            bullet("Tournament-legal", p.get("valid")),
            bullet("Owner", p.get("owner")),
            "",
            "## Decklist",
        ]
        for e in p["entries"]:
            cost = f"{e['cost']} ink" if e["cost"] is not None else "-"
            lines.append(f"- {e['quantity']}x **{e['name']}** ({cost}) `{e['definition_id']}`")
        return join_lines(lines)

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_create_deck",
    annotations={
        "title": "Create a Deck",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_create_deck(
    ctx: Context,
    name: Annotated[
        str, Field(description="Name for the new deck.", min_length=1, max_length=80)
    ],
    card_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Optional starting contents as definitionIds, one entry per copy "
                "(e.g. ['10-71','10-71','10-71','10-71', ...]). A legal deck is exactly "
                "60 cards with at most 4 copies of any card. Omit to create an empty deck."
            ),
            max_length=200,
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Creates a new deck on your account and returns its id.

    Creates it empty and then fills it when card_ids is given, which is the two
    steps Duels.ink needs. Requires a session cookie.

    Do NOT use to change an existing deck (duels_update_deck), and do NOT pass
    card names - card_ids takes catalog ids like '10-71'. Use
    duels_import_decklist when you have a text decklist with names.

    Examples:
    - "Create an empty deck called Ramp v2" -> name='Ramp v2'
    - "Save this list as Amber Aggro" -> name='Amber Aggro', card_ids=[...]

    Args:
        ctx (Context): Injected by FastMCP.
        name (str): Deck name.
        card_ids (Optional[list[str]]): Starting contents as definitionIds.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"id","name","card_count","colors","valid", ...} for the new deck.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Creating a deck")

    created = await app.client.post("/api/decks", {"name": name})
    deck = (created or {}).get("deck") or created or {}
    deck_id = deck.get("id")
    if not deck_id:
        raise DuelsError(f"Duels.ink did not return a deck id: {created}")

    if card_ids:
        updated = await app.client.request(
            "PATCH", f"/api/decks/{deck_id}", json_body={"cardIds": card_ids}
        )
        deck = (updated or {}).get("deck") or deck

    payload = _deck_summary(deck)

    def md(p: dict) -> str:
        return join_lines(
            [
                f"# Deck created: {p.get('name')}",
                "",
                bullet("Id", p.get("id")),
                bullet("Cards", p.get("card_count")),
                bullet("Tournament-legal", p.get("valid")),
            ]
        )

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_update_deck",
    annotations={
        "title": "Rename or Recontent a Deck",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_update_deck(
    ctx: Context,
    deck_id: DeckId,
    name: Annotated[
        Optional[str],
        Field(default=None, description="New name. Omit to leave it unchanged.", max_length=80),
    ] = None,
    card_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "The deck's new complete contents as definitionIds, one entry per copy. "
                "This REPLACES the deck's cards - it is not a merge, so read the current "
                "list with duels_get_deck first if you only mean to add or remove a few."
            ),
            max_length=200,
        ),
    ] = None,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Renames a deck and/or replaces its card list.

    `card_ids` replaces the whole deck rather than merging, so fetch the current
    list with duels_get_deck, change it, and send the full result back.

    Do NOT use to delete a deck (duels_delete_deck).

    Examples:
    - "Rename it to Tourmaline v3" -> deck_id, name='Tourmaline v3'
    - "Swap two Flotsam for two Mushu" -> read duels_get_deck, edit the list, send the whole card_ids back

    Args:
        ctx (Context): Injected by FastMCP.
        deck_id (str): Deck UUID.
        name (Optional[str]): New name.
        card_ids (Optional[list[str]]): Complete replacement contents.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The deck's updated summary.

    Error Handling:
        Returns an error when neither name nor card_ids is supplied.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Updating a deck")
    if name is None and card_ids is None:
        raise DuelsError("Pass name and/or card_ids - there is nothing to update otherwise.")

    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if card_ids is not None:
        body["cardIds"] = card_ids

    updated = await app.client.request("PATCH", f"/api/decks/{deck_id}", json_body=body)
    deck = (updated or {}).get("deck") or updated or {}
    payload = _deck_summary(deck)

    def md(p: dict) -> str:
        return join_lines(
            [
                f"# Deck updated: {p.get('name')}",
                "",
                bullet("Cards", p.get("card_count")),
                bullet("Colors", [str(c).title() for c in p.get("colors") or []]),
                bullet("Tournament-legal", p.get("valid")),
            ]
        )

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_delete_deck",
    annotations={
        "title": "Delete a Deck",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_delete_deck(
    ctx: Context,
    deck_id: DeckId,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Permanently deletes one of your decks. This cannot be undone.

    Confirm with the person which deck they mean before calling this - check
    the name with duels_get_deck first, since deck ids are opaque UUIDs and
    there is no recovery.

    Do NOT use this to change a deck's contents or name - duels_update_deck
    does that without destroying anything. Do NOT use it on a deck you did not
    create: only your own decks can be deleted.

    Examples:
    - "Delete the deck I just imported" -> confirm the name with duels_get_deck first, then deck_id

    Args:
        ctx (Context): Injected by FastMCP.
        deck_id (str): Deck UUID to delete.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"success": bool, "deck_id": str}.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Deleting a deck")
    result = await app.client.delete(f"/api/decks/{deck_id}")
    payload = {"success": bool((result or {}).get("success", True)), "deck_id": deck_id}
    return render(
        payload,
        response_format,
        lambda p: f"Deck `{p['deck_id']}` deleted." if p["success"] else "Delete failed.",
    )


@mcp.tool(
    name="duels_import_decklist",
    annotations={
        "title": "Import a Text Decklist",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_import_decklist(
    ctx: Context,
    name: Annotated[str, Field(description="Name for the imported deck.", min_length=1, max_length=80)],
    decklist: Annotated[
        str,
        Field(
            description=(
                "The decklist as text, one card per line, quantity first. Both "
                "'4 Flotsam - Slippery as an Eel' and '4 10-71' work, with or without "
                "the 'x' in '4x'."
            ),
            min_length=3,
            max_length=8000,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Creates a deck from a pasted text decklist, resolving card names to catalog ids.

    Use for lists copied from Dreamborn, a tournament report or a friend. Lines
    look like "4 Flotsam - Slippery as an Eel"; catalog ids such as "4 10-71"
    work too. Names are matched against the catalog, and anything that cannot be
    matched is reported rather than silently dropped, so you can see exactly
    what is missing.

    Do NOT use when you already have definitionIds - duels_create_deck takes
    them directly without a name lookup.

    Examples:
    - "Import this as Ramp v2: 4 Flotsam - Slippery as an Eel ..." -> name='Ramp v2', decklist='...'
    - "Load the list from this tournament report" -> paste the text straight into decklist

    Args:
        ctx (Context): Injected by FastMCP.
        name (str): Name for the new deck.
        decklist (str): The text decklist.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {"deck": {...}, "imported": int, "unmatched": [str],
        "resolved": [{"line","name","definition_id","quantity"}]}.

    Error Handling:
        Returns an error when no line could be parsed at all. A partially
        matched list still creates the deck and lists the unmatched lines.
    """
    app = app_ctx(ctx)
    app.client.require_auth("Importing a decklist")

    card_ids: list[str] = []
    resolved_rows: list[dict] = []
    unmatched: list[str] = []

    for raw_line in decklist.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        match = DECKLIST_LINE.match(line)
        if not match:
            unmatched.append(line)
            continue
        quantity, token = int(match.group(1)), match.group(2).strip()

        # A bare catalog id needs no lookup.
        if re.fullmatch(r"\d{1,2}-\w{1,8}", token):
            definition_id, display = token, token
        else:
            found, _ = await app.catalog.search(query=token, limit=5)
            best = None
            for candidate in found:
                full = str(candidate.get("fullName") or "").lower()
                if full == token.lower() or full.startswith(token.lower()):
                    best = candidate
                    break
            best = best or (found[0] if found else None)
            if not best:
                unmatched.append(line)
                continue
            definition_id = best.get("id")
            display = best.get("fullName")

        card_ids.extend([definition_id] * quantity)
        resolved_rows.append(
            {
                "line": line,
                "name": display,
                "definition_id": definition_id,
                "quantity": quantity,
            }
        )

    if not card_ids:
        raise DuelsError(
            "No decklist lines could be parsed. Each line should be a quantity then a card, "
            "for example '4 Flotsam - Slippery as an Eel'."
        )

    created = await app.client.post("/api/decks", {"name": name})
    deck = (created or {}).get("deck") or created or {}
    deck_id = deck.get("id")
    if not deck_id:
        raise DuelsError(f"Duels.ink did not return a deck id: {created}")
    updated = await app.client.request(
        "PATCH", f"/api/decks/{deck_id}", json_body={"cardIds": card_ids}
    )
    deck = (updated or {}).get("deck") or deck

    payload = {
        "deck": _deck_summary(deck),
        "imported": len(card_ids),
        "unmatched": unmatched,
        "resolved": resolved_rows,
    }

    def md(p: dict) -> str:
        lines = [
            f"# Imported: {p['deck'].get('name')}",
            "",
            bullet("Deck id", p["deck"].get("id")),
            bullet("Cards imported", p["imported"]),
            bullet("Tournament-legal", p["deck"].get("valid")),
        ]
        if p["unmatched"]:
            lines += [
                "",
                "## Lines that could not be matched",
                *[f"- `{u}`" for u in p["unmatched"]],
            ]
        return join_lines(lines)

    return render(payload, response_format, md)
