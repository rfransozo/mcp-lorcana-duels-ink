"""Reusable annotated parameter types and enums for every duels_mcp tool.

Tools declare their arguments **flat** (one function parameter per field)
rather than wrapping them in a single Pydantic model. Both approaches validate
identically, but flat parameters produce a flat JSON schema, so an agent sees
`{"query": "...", "limit": 5}` instead of `{"params": {...}}`. The shared
`Annotated` aliases below keep that from turning into copy-paste.
"""

from enum import Enum
from typing import Annotated, Any, Optional

from pydantic import Field


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class BotDifficulty(str, Enum):
    """Difficulty of the built-in Duels.ink AI opponent."""

    EASY = "easy"
    NORMAL = "normal"
    DIFFICULT = "difficult"


class LegalityFormat(str, Enum):
    """Duels.ink deck legality formats."""

    CORE = "core"
    INFINITY = "infinity"
    CORE_JA = "core_ja"


# -----------------------------------------------------------------
# Shared parameter aliases
# -----------------------------------------------------------------
ResponseFmt = Annotated[
    ResponseFormat,
    Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for human-readable output, 'json' for machine-readable output.",
    ),
]

Limit = Annotated[
    int,
    Field(default=25, ge=1, le=100, description="Maximum number of results to return (1-100)."),
]

Offset = Annotated[
    int,
    Field(
        default=0,
        ge=0,
        description="Number of results to skip, for paging through large result sets.",
    ),
]

GameId = Annotated[
    str,
    Field(
        description=(
            "Game UUID, from duels_start_bot_game or duels_list_active_games "
            "(e.g. '01a0bc5a-896c-715e-a447-eb939dc3707a')."
        ),
        min_length=8,
        max_length=64,
    ),
]

TableId = Annotated[
    str,
    Field(
        description="Table UUID, from duels_create_table or a shared table link.",
        min_length=8,
        max_length=64,
    ),
]

CardInstanceId = Annotated[
    str,
    Field(
        description=(
            "The card's instanceId within this game - NOT its definitionId. "
            "Read it from duels_get_game_state; every hand and board card carries one "
            "(e.g. '01a0bc5a-c80f-7855-b0d4-862293642cde')."
        ),
        min_length=8,
        max_length=64,
    ),
]

DeckId = Annotated[
    str,
    Field(
        description="Deck UUID, from duels_list_my_decks or duels_browse_public_decks.",
        min_length=8,
        max_length=64,
    ),
]

DeckCardIds = Annotated[
    list[str],
    Field(
        description=(
            "The 60 card definitionIds making up the deck, one entry per physical copy "
            "(e.g. ['10-71','10-71','10-71','10-71','11-97', ...]). Lorcana decks are "
            "exactly 60 cards with at most 4 copies of any card."
        ),
        min_length=1,
        max_length=200,
    ),
]

SkipTriggers = Annotated[
    bool,
    Field(
        default=False,
        description=(
            "Skip optional 'you may' triggered abilities on this action instead of being "
            "prompted for each one. Leave False unless you deliberately want to decline them."
        ),
    ),
]

__all__ = [
    "ResponseFormat",
    "BotDifficulty",
    "LegalityFormat",
    "ResponseFmt",
    "Limit",
    "Offset",
    "GameId",
    "TableId",
    "CardInstanceId",
    "DeckId",
    "DeckCardIds",
    "SkipTriggers",
    "Annotated",
    "Optional",
    "Any",
    "Field",
]
