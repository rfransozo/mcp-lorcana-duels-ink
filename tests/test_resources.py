"""MCP resources, and the card renderings that only markdown reaches.

Resources are the half of the surface that is addressed by URI rather than
called, and nothing exercised them: every one of them could have been broken
and the whole suite would still have passed.
"""

import json

import pytest

from tests.conftest import call_text
from tests.fixtures import decks as deck_fixtures
from tests.fixtures import games
from tests.test_gamews import FakeGameServer

pytestmark = pytest.mark.anyio

LOGS = [
    {
        "id": "l-1",
        "turnNumber": 3,
        "player": 2,
        "type": "CARD_PLAYED",
        "message": "You played {card:0} (cost 3)",
        "cardRefs": [{"id": "10-103", "name": "Mushu - Stealthy Dragon"}],
    },
    {
        "id": "l-2",
        "turnNumber": 3,
        "player": 2,
        "type": "TIMER_STARTED",
        "message": "Your timer started (2:00)",
        "data": {"timeRemainingMs": 120000},
    },
]


async def read(mcp_client, uri: str):
    contents = await mcp_client.read_resource(uri)
    return json.loads(contents[0].text)


class TestCardResource:
    async def test_a_card_is_addressable_by_its_catalog_id(self, mcp_client):
        data = await read(mcp_client, "duels://cards/10-71")
        assert data["fullName"] == "Flotsam - Slippery as an Eel"

    async def test_an_unknown_id_answers_rather_than_raising(self, mcp_client):
        """A resource read has nowhere to put an exception, so the miss has to
        be part of the payload."""
        data = await read(mcp_client, "duels://cards/99-999")
        assert "No card with id" in data["error"]


class TestDeckResource:
    async def test_card_ids_are_resolved_to_names(self, mcp_client):
        data = await read(mcp_client, f"duels://deck/{deck_fixtures.DECK_ID}")
        assert data["name"] == "Tourmaline v2"
        assert data["card_count"] == 12
        assert data["cards"]["10-71"]["fullName"] == "Flotsam - Slippery as an Eel"

    async def test_the_flat_id_list_is_kept_alongside(self, mcp_client):
        """Counting copies needs the repeats, which the resolved map loses."""
        data = await read(mcp_client, f"duels://deck/{deck_fixtures.DECK_ID}")
        assert data["card_ids"].count("10-71") == 4
        assert len(data["cards"]) < len(data["card_ids"])


class TestGameResources:
    async def test_the_state_reads_like_the_tool(self, router, mcp_client):
        async with FakeGameServer() as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            data = await read(mcp_client, f"duels://game/{games.GAME_ID}/state")
            assert data["game_id"] == games.GAME_ID
            assert "legal_moves" in data and "me" in data

    async def test_the_log_substitutes_card_placeholders(self, router, mcp_client):
        """A log full of {card:0} is unreadable, and the names live in a
        parallel cardRefs list."""
        async with FakeGameServer(logs=LOGS) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            data = await read(mcp_client, f"duels://game/{games.GAME_ID}/log")
            messages = [e["message"] for e in data["entries"]]
            assert "You played Mushu - Stealthy Dragon (cost 3)" in messages
            assert not any("{card:" in m for m in messages)

    async def test_the_log_keeps_turn_and_player(self, router, mcp_client):
        async with FakeGameServer(logs=LOGS) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            data = await read(mcp_client, f"duels://game/{games.GAME_ID}/log")
            assert data["entries"][0]["turn"] == 3
            assert data["entries"][0]["player"] == 2


class TestCardMarkdown:
    """The markdown renderings, which every existing test skipped by asking
    for json."""

    async def test_a_card_shows_its_flavour_image_and_legality(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_card", definition_id="10-71")
        assert "Legal in" in text
        assert "[Card image](" in text
        assert ">" in text, "flavour text should be quoted"

    async def test_an_uninkable_card_says_so(self, mcp_client):
        """Inkability decides whether a card can pay for anything, so it is
        worth a word rather than a missing field."""
        text = await call_text(mcp_client, "duels_get_card", definition_id="10-103")
        assert "not inkable" in text

    async def test_resolving_ids_renders_a_readable_list(self, mcp_client):
        text = await call_text(
            mcp_client, "duels_resolve_cards", definition_ids=["10-71", "13-80"]
        )
        assert "Flotsam - Slippery as an Eel" in text
        assert "Rapunzel - Tower Defender" in text
        assert "ink" in text and "lore" in text

    async def test_ids_that_resolve_to_nothing_are_named(self, mcp_client):
        """Silently returning fewer cards than were asked for would hide a
        typo in a decklist."""
        text = await call_text(
            mcp_client, "duels_resolve_cards", definition_ids=["10-71", "99-999"]
        )
        assert "Unresolved ids" in text and "99-999" in text
