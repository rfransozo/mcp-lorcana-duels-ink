"""Card catalog tools - search, single lookup and batch id resolution."""

from typing import Annotated, Optional

from fastmcp import Context
from pydantic import Field

from ..app import app_ctx, mcp
from ..cards import summarise
from ..formatting import empty_result, join_lines, paginate, pagination_footer, render
from ..models import Limit, Offset, ResponseFmt, ResponseFormat
from ..toolkit import tool_errors

CARD_TYPES = ("character", "action", "item", "location", "song")
INK_COLORS = ("amber", "amethyst", "emerald", "ruby", "sapphire", "steel")


def _card_md(card: dict) -> str:
    """Heading plus stat line plus rules text for one card."""
    head = f"### {card.get('fullName') or card.get('name')}  `{card.get('id')}`"
    facts = []
    if card.get("type"):
        facts.append(str(card["type"]).title())
    if card.get("colors"):
        facts.append("/".join(str(c).title() for c in card["colors"]))
    if card.get("cost") is not None:
        facts.append(f"{card['cost']} ink")
    if card.get("inkable") is False:
        facts.append("not inkable")
    if card.get("strength") is not None and card.get("willpower") is not None:
        facts.append(f"{card['strength']}/{card['willpower']}")
    if card.get("lore"):
        facts.append(f"{card['lore']} lore")
    if card.get("rarity"):
        facts.append(str(card["rarity"]).title())

    lines = [head, " - ".join(facts) if facts else ""]
    if card.get("subtypes"):
        lines.append(f"_{', '.join(str(s) for s in card['subtypes'])}_")
    if card.get("rulesText"):
        lines.append(f"\n{card['rulesText']}")
    return join_lines(lines)


@mcp.tool(
    name="duels_search_cards",
    annotations={
        "title": "Search Lorcana Cards",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_search_cards(
    ctx: Context,
    query: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Free text matched against card names and rules text "
                "(e.g. 'Mickey Mouse', 'Flotsam', 'Evasive')."
            ),
            max_length=100,
        ),
    ] = None,
    set_number: Annotated[
        Optional[int],
        Field(
            default=None,
            description="Restrict to one set, e.g. 10. Sets run from 1 upward.",
            ge=1,
            le=30,
        ),
    ] = None,
    card_type: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Card type: 'character', 'action', 'item', 'location' or 'song'.",
        ),
    ] = None,
    color: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Ink colour: 'amber', 'amethyst', 'emerald', 'ruby', 'sapphire' or 'steel'."
            ),
        ),
    ] = None,
    cost: Annotated[
        Optional[int],
        Field(default=None, description="Exact ink cost to match.", ge=0, le=20),
    ] = None,
    inkable: Annotated[
        Optional[bool],
        Field(
            default=None,
            description="True for cards that can go into the inkwell, False for those that cannot.",
        ),
    ] = None,
    rarity: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Rarity, e.g. 'common', 'uncommon', 'rare', 'super_rare', 'legendary'.",
        ),
    ] = None,
    legality: Annotated[
        Optional[str],
        Field(default=None, description="Restrict to a format: 'core', 'infinity' or 'core_ja'."),
    ] = None,
    limit: Limit = 25,
    offset: Offset = 0,
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns Lorcana cards matching a text query and/or filters, from the ~3,200-card Duels.ink catalog.

    Use for deckbuilding and for any "what does this card do" question. Filters
    combine with AND. Works without authentication.

    Do NOT use to turn ids found in a live game into names - duels_resolve_cards
    batches that far more cheaply. Do NOT use to list saved decks
    (duels_list_my_decks) or community decks (duels_browse_public_decks).

    Args:
        ctx (Context): Injected by FastMCP.
        query (Optional[str]): Free text over names and rules text.
        set_number (Optional[int]): Restrict to one set (1-30).
        card_type (Optional[str]): character | action | item | location | song.
        color (Optional[str]): amber | amethyst | emerald | ruby | sapphire | steel.
        cost (Optional[int]): Exact ink cost.
        inkable (Optional[bool]): Inkwell eligibility.
        rarity (Optional[str]): Rarity tier.
        legality (Optional[str]): core | infinity | core_ja.
        limit (int): 1-100, default 25.
        offset (int): Pagination offset, default 0.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: In JSON mode:
        {
            "total": int, "count": int, "offset": int,
            "has_more": bool, "next_offset": int | null,
            "cards": [
                {
                    "id": str,        # definitionId, e.g. "10-71"
                    "fullName": str,  # "Flotsam - Slippery as an Eel"
                    "type": str, "cost": int, "inkable": bool,
                    "colors": list[str], "strength": int, "willpower": int,
                    "lore": int, "rarity": str, "subtypes": list[str],
                    "rulesText": str
                }
            ]
        }

    Examples:
        - "Find Evasive characters that cost 3" -> query='Evasive', cost=3
        - "Show me ruby songs" -> color='ruby', card_type='song'
        - "What is Flotsam?" -> query='Flotsam'

    Error Handling:
        Returns "No cards found..." when nothing matches. Invalid card_type or
        color values are rejected with the list of accepted values.
    """
    app = app_ctx(ctx)

    if card_type and card_type.lower() not in CARD_TYPES:
        raise ValueError(f"card_type must be one of: {', '.join(CARD_TYPES)}")
    if color and color.lower() not in INK_COLORS:
        raise ValueError(f"color must be one of: {', '.join(INK_COLORS)}")

    cards, total = await app.catalog.search(
        query=query,
        set_number=set_number,
        card_type=card_type,
        color=color,
        cost=cost,
        inkable=inkable,
        rarity=rarity,
        legality=legality,
        limit=limit,
        offset=offset,
    )

    if not cards:
        return empty_result(
            "cards",
            "Try a broader query, drop a filter, or check the spelling of the card name.",
        )

    payload = paginate([summarise(c) for c in cards], total, offset, key="cards")

    def md(p: dict) -> str:
        body = "\n\n".join(_card_md(c) for c in p["cards"])
        return join_lines(["# Card search results", "", body, "", pagination_footer(p)])

    return render(payload, response_format, md)


@mcp.tool(
    name="duels_get_card",
    annotations={
        "title": "Get One Lorcana Card",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_get_card(
    ctx: Context,
    definition_id: Annotated[
        str,
        Field(
            description=(
                "The card's catalog id, also called definitionId in game states "
                "(e.g. '10-71' = set 10, card 71)."
            ),
            min_length=2,
            max_length=32,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns the complete record for one card given its definitionId, such as "10-71".

    Use when you already have an exact id - from a game state, a decklist or a
    previous search - and want everything about it including rules text, flavour
    text and image URL.

    Do NOT use to look a card up by name (use duels_search_cards) or to resolve
    many ids at once (use duels_resolve_cards, which batches).

    Args:
        ctx (Context): Injected by FastMCP.
        definition_id (str): Catalog id in '<set>-<number>' form, e.g. '10-71'.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: The full card record - id, fullName, type, cost, inkable, colors,
        strength, willpower, lore, moveCost, rarity, legality, subtypes,
        abilities, rulesText, flavorText, imageUrl - or a "not found" message
        explaining the id format.

    Examples:
        - "What is card 10-71?" -> definition_id='10-71'
        - "Details for the hand card with definitionId 11-97" -> definition_id='11-97'
    """
    app = app_ctx(ctx)
    card = await app.catalog.get(definition_id)
    if not card:
        return empty_result(
            f"card with id '{definition_id}'",
            "Ids look like '<set>-<number>', e.g. '10-71'. "
            "Use duels_search_cards to find it by name.",
        )

    def md(p: dict) -> str:
        extra = []
        if p.get("legality"):
            extra.append(f"- **Legal in**: {', '.join(str(x) for x in p['legality'])}")
        if p.get("flavorText"):
            extra.append(f"\n> {p['flavorText']}")
        if p.get("imageUrl"):
            extra.append(f"\n[Card image]({p['imageUrl']})")
        return join_lines([_card_md(p), "", *extra])

    return render(card, response_format, md)


@mcp.tool(
    name="duels_resolve_cards",
    annotations={
        "title": "Resolve Card Ids to Names",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
@tool_errors
async def duels_resolve_cards(
    ctx: Context,
    definition_ids: Annotated[
        list[str],
        Field(
            description=(
                "Catalog ids to resolve, e.g. ['10-71', '11-97', '12-133']. These are the "
                "definitionId values carried by cards in a game state or decklist."
            ),
            min_length=1,
            max_length=120,
        ),
    ],
    response_format: ResponseFmt = ResponseFormat.MARKDOWN,
) -> str:
    """Returns name and key stats for a batch of definitionIds in a single call.

    Use when reading raw data - a decklist, a replay, another tool's JSON - that
    refers to cards only by id. Resolving 60 ids costs a handful of requests
    rather than 60, because the catalog is fetched one set at a time.

    Do NOT use for a single card when you also want flavour text and the image
    (duels_get_card), and do NOT use it to search by name (duels_search_cards).
    You rarely need it for gameplay: duels_get_game_state already resolves names.

    Args:
        ctx (Context): Injected by FastMCP.
        definition_ids (list[str]): 1-120 catalog ids.
        response_format (ResponseFormat): 'markdown' (default) or 'json'.

    Returns:
        str: {
            "resolved": {"<id>": {id, fullName, type, cost, strength, willpower,
                                  lore, inkable, colors, rarity, subtypes,
                                  rulesText} | null},
            "unresolved": list[str]   # ids with no catalog match
        }
        Unknown ids are listed rather than silently dropped.

    Examples:
        - "What are cards 10-71 and 11-97?" -> definition_ids=['10-71','11-97']
        - "Name every card in this decklist" -> pass the whole id list
    """
    app = app_ctx(ctx)
    resolved = await app.catalog.resolve_many(definition_ids)

    payload = {
        "resolved": {k: (summarise(v) if v else None) for k, v in resolved.items()},
        "unresolved": [k for k, v in resolved.items() if not v],
    }

    def md(p: dict) -> str:
        lines = ["# Resolved cards", ""]
        for cid, card in p["resolved"].items():
            if not card:
                continue
            facts = []
            if card.get("cost") is not None:
                facts.append(f"{card['cost']} ink")
            if card.get("type"):
                facts.append(str(card["type"]))
            if card.get("strength") is not None and card.get("willpower") is not None:
                facts.append(f"{card['strength']}/{card['willpower']}")
            if card.get("lore"):
                facts.append(f"{card['lore']} lore")
            lines.append(f"- `{cid}` **{card['fullName']}** ({', '.join(facts)})")
        if p["unresolved"]:
            lines.append("")
            lines.append(f"_Unresolved ids: {', '.join(p['unresolved'])}_")
        return join_lines(lines)

    return render(payload, response_format, md)
