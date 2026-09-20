"""Deck fixtures.

`cardIds` is a flat list with one entry per physical copy, which is how
Duels.ink stores a decklist.
"""

DECK_ID = "01a09330-1921-77c0-8a64-db4aad85e1fd"
PUBLIC_DECK_ID = "suggested-curator-library-1-set12"

# 12 cards rather than 60 - enough to exercise grouping and counting.
CARD_IDS = (
    ["10-71"] * 4
    + ["11-97"] * 4
    + ["10-45"] * 3
    + ["13-80"] * 1
)


def summary(deck_id: str = DECK_ID, **overrides) -> dict:
    """A deck as it appears in a list response."""
    deck = {
        "id": deck_id,
        "name": "Tourmaline v2",
        "cardCount": len(CARD_IDS),
        "colors": ["emerald", "amethyst"],
        "secondaryColors": [],
        "legalFormats": ["CoreConstructed", "InfinityConstructed"],
        "folderId": None,
        "visibility": "private",
        "hideOwner": False,
        "likeCount": 0,
        "viewCount": 0,
        "copyCount": 0,
        "valid": True,
        "hasCustomCards": False,
        "type": "constructed",
        "updatedAt": "2026-09-12T21:32:49.550Z",
        "createdAt": "2026-09-12T01:16:38.049Z",
    }
    deck.update(overrides)
    return deck


def detail(deck_id: str = DECK_ID, **overrides) -> dict:
    """A deck detail response: {"deck": {..., "cardIds": [...]}}."""
    deck = summary(deck_id, **overrides)
    deck["cardIds"] = list(CARD_IDS)
    deck["ownerName"] = "Fransozo"
    deck["isOwner"] = True
    return {"deck": deck}


MY_DECKS = {"decks": [summary(), summary("deck-2", name="Princess Power v2")]}

PUBLIC_DECKS = {
    "decks": [
        summary(
            PUBLIC_DECK_ID,
            name="Curator's Library 1",
            visibility="public",
            ownerName="curator",
            likeCount=42,
            valid=False,
        )
    ]
}
