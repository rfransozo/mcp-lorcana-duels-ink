"""Building decks, and the modes that hand you one.

Deck writes are the only tools here that destroy something, so the cases that
matter are the ones where a wrong call would be expensive: an update with
nothing to update, an import that silently drops half a list, a create whose
second request fails after the deck already exists.
"""

import httpx
import pytest

from tests.conftest import call_json, call_text
from tests.fixtures import decks as deck_fixtures

pytestmark = pytest.mark.anyio

NEW_DECK_ID = "01a09330-0000-77c0-8a64-000000000001"


def created(deck_id: str = NEW_DECK_ID, **overrides) -> dict:
    return {"deck": deck_fixtures.summary(deck_id, **overrides)}


class TestListingDecks:
    async def test_your_decks_need_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_list_my_decks")
        assert text.startswith("Error:")

    async def test_your_decks_are_summarised(self, authed_mcp_client):
        text = await call_text(authed_mcp_client, "duels_list_my_decks")
        assert "Tourmaline v2" in text
        assert "Emerald/Amethyst" in text, "colours should read as names, not raw json"
        assert "12 cards" in text

    async def test_an_empty_account_says_how_to_start(self, router, authed_mcp_client):
        router.json_on("/api/decks", {"decks": []})
        text = await call_text(authed_mcp_client, "duels_list_my_decks")
        assert "duels_create_deck" in text

    async def test_paging_reports_what_is_left(self, authed_mcp_client):
        data = await call_json(authed_mcp_client, "duels_list_my_decks", limit=1)
        assert data["count"] == 1
        assert data["has_more"] is True
        assert data["next_offset"] == 1

    async def test_an_illegal_deck_is_flagged(self, router, authed_mcp_client):
        """A deck you cannot queue with is the one fact worth seeing in a
        list."""
        router.json_on("/api/decks", {"decks": [deck_fixtures.summary(valid=False)]})
        text = await call_text(authed_mcp_client, "duels_list_my_decks")
        assert "not tournament-legal" in text

    async def test_public_decks_work_anonymously(self, mcp_client):
        text = await call_text(mcp_client, "duels_browse_public_decks")
        assert not text.startswith("Error:")


class TestCreatingADeck:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_create_deck", name="Test")
        assert text.startswith("Error:")

    async def test_an_empty_deck_is_one_request(self, router, authed_mcp_client):
        router.json_on("/api/decks", created())
        text = await call_text(authed_mcp_client, "duels_create_deck", name="Test")
        assert "Deck created" in text and NEW_DECK_ID in text
        assert [c.url.path for c in router.calls].count("/api/decks") == 1

    async def test_cards_are_filled_in_by_a_second_patch(self, router, authed_mcp_client):
        """Duels.ink creates a deck empty and takes its contents separately."""
        router.json_on("/api/decks", created())
        router.on(
            f"/api/decks/{NEW_DECK_ID}",
            lambda _r: httpx.Response(200, json=created(cardCount=4)),
        )
        await call_text(
            authed_mcp_client, "duels_create_deck", name="Test", card_ids=["10-71"] * 4
        )
        patch = router.calls[-1]
        assert patch.method == "PATCH"
        assert b'"cardIds"' in patch.content

    async def test_a_create_that_returns_no_id_is_an_error(self, router, authed_mcp_client):
        """Patching contents onto a deck with no id would silently write
        nowhere."""
        router.json_on("/api/decks", {"deck": {"name": "Test"}})
        text = await call_text(authed_mcp_client, "duels_create_deck", name="Test")
        assert text.startswith("Error:") and "did not return a deck id" in text


class TestUpdatingADeck:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(
            mcp_client, "duels_update_deck", deck_id=deck_fixtures.DECK_ID, name="x"
        )
        assert text.startswith("Error:")

    async def test_nothing_to_update_is_refused_before_the_call(
        self, router, authed_mcp_client
    ):
        """An empty PATCH would look like it worked and change nothing."""
        before = len(router.calls)
        text = await call_text(
            authed_mcp_client, "duels_update_deck", deck_id=deck_fixtures.DECK_ID
        )
        assert text.startswith("Error:") and "name and/or card_ids" in text
        assert len(router.calls) == before, "nothing should have been sent"

    async def test_a_rename_sends_only_the_name(self, router, authed_mcp_client):
        router.on(
            f"/api/decks/{deck_fixtures.DECK_ID}",
            lambda _r: httpx.Response(200, json=created(deck_fixtures.DECK_ID, name="Renamed")),
        )
        text = await call_text(
            authed_mcp_client, "duels_update_deck", deck_id=deck_fixtures.DECK_ID, name="Renamed"
        )
        assert "Deck updated: Renamed" in text
        assert b'"cardIds"' not in router.calls[-1].content

    async def test_recontenting_replaces_the_whole_list(self, router, authed_mcp_client):
        """cardIds is a replacement, not an append, and the docstring says so."""
        router.on(
            f"/api/decks/{deck_fixtures.DECK_ID}",
            lambda _r: httpx.Response(200, json=created(deck_fixtures.DECK_ID, cardCount=2)),
        )
        await call_text(
            authed_mcp_client,
            "duels_update_deck",
            deck_id=deck_fixtures.DECK_ID,
            card_ids=["10-71", "11-97"],
        )
        assert b'"cardIds"' in router.calls[-1].content
        assert b'"name"' not in router.calls[-1].content


class TestDeletingADeck:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(
            mcp_client, "duels_delete_deck", deck_id=deck_fixtures.DECK_ID
        )
        assert text.startswith("Error:")

    async def test_a_delete_is_confirmed_by_id(self, router, authed_mcp_client):
        router.on(
            f"/api/decks/{deck_fixtures.DECK_ID}",
            lambda _r: httpx.Response(200, json={"success": True}),
        )
        text = await call_text(
            authed_mcp_client, "duels_delete_deck", deck_id=deck_fixtures.DECK_ID
        )
        assert "deleted" in text and deck_fixtures.DECK_ID in text
        assert router.calls[-1].method == "DELETE"

    async def test_a_refused_delete_does_not_claim_success(self, router, authed_mcp_client):
        router.on(
            f"/api/decks/{deck_fixtures.DECK_ID}",
            lambda _r: httpx.Response(200, json={"success": False}),
        )
        text = await call_text(
            authed_mcp_client, "duels_delete_deck", deck_id=deck_fixtures.DECK_ID
        )
        assert "Delete failed" in text


class TestImportingADecklist:
    """The one tool that has to understand what a person typed."""

    def _routes(self, router):
        router.json_on("/api/decks", created())
        router.on(
            f"/api/decks/{NEW_DECK_ID}",
            lambda _r: httpx.Response(200, json=created(cardCount=8)),
        )

    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(
            mcp_client, "duels_import_decklist", name="T", decklist="4 Flotsam"
        )
        assert text.startswith("Error:")

    async def test_names_are_resolved_and_quantities_expanded(
        self, router, authed_mcp_client
    ):
        self._routes(router)
        data = await call_json(
            authed_mcp_client,
            "duels_import_decklist",
            name="T",
            decklist="4 Flotsam - Slippery as an Eel\n2 Rapunzel - Tower Defender",
        )
        assert data["imported"] == 6
        by_name = {r["name"]: r for r in data["resolved"]}
        assert by_name["Flotsam - Slippery as an Eel"]["definition_id"] == "10-71"
        assert by_name["Rapunzel - Tower Defender"]["quantity"] == 2

    async def test_a_bare_catalog_id_skips_the_lookup(self, router, authed_mcp_client):
        """'4 10-71' is unambiguous, and searching for it would find nothing."""
        self._routes(router)
        data = await call_json(
            authed_mcp_client, "duels_import_decklist", name="T", decklist="4 10-71"
        )
        assert data["imported"] == 4
        assert data["resolved"][0]["definition_id"] == "10-71"
        assert not any(c.url.path == "/api/cards" for c in router.calls)

    async def test_comments_and_blank_lines_are_ignored(self, router, authed_mcp_client):
        self._routes(router)
        data = await call_json(
            authed_mcp_client,
            "duels_import_decklist",
            name="T",
            decklist="# my deck\n\n4 10-71\n// a note\n",
        )
        assert data["imported"] == 4
        assert data["unmatched"] == []

    async def test_a_line_that_resolves_to_nothing_is_reported_not_dropped(
        self, router, authed_mcp_client
    ):
        """Silently importing 59 of 60 cards is worse than saying which one
        was lost."""
        self._routes(router)
        data = await call_json(
            authed_mcp_client,
            "duels_import_decklist",
            name="T",
            decklist="4 10-71\n3 Nonexistent Card Xyzzy",
        )
        assert data["imported"] == 4
        assert data["unmatched"] == ["3 Nonexistent Card Xyzzy"]

    async def test_unmatched_lines_are_visible_in_markdown_too(
        self, router, authed_mcp_client
    ):
        self._routes(router)
        text = await call_text(
            authed_mcp_client,
            "duels_import_decklist",
            name="T",
            decklist="4 10-71\n3 Nonexistent Card Xyzzy",
        )
        assert "could not be matched" in text and "Xyzzy" in text

    async def test_a_decklist_nothing_parses_from_explains_the_format(
        self, router, authed_mcp_client
    ):
        text = await call_text(
            authed_mcp_client,
            "duels_import_decklist",
            name="T",
            decklist="just some prose with no quantities",
        )
        assert text.startswith("Error:")
        assert "quantity then a card" in text

    async def test_nothing_is_created_when_nothing_parses(self, router, authed_mcp_client):
        """A deck named after a failed import would be left behind empty."""
        before = [c.url.path for c in router.calls].count("/api/decks")
        await call_text(
            authed_mcp_client, "duels_import_decklist", name="T", decklist="nonsense"
        )
        assert [c.url.path for c in router.calls].count("/api/decks") == before


class TestOtherModes:
    async def test_a_puzzle_is_fetched_by_id(self, router, mcp_client):
        router.json_on("/api/puzzle/daily-01", {"id": "daily-01", "name": "Lethal on board"})
        text = await call_text(mcp_client, "duels_get_puzzle", puzzle_id="daily-01")
        assert "daily-01" in text

    async def test_draft_decks_need_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_list_draft_decks")
        assert text.startswith("Error:")

    async def test_no_draft_decks_says_so(self, router, authed_mcp_client):
        router.json_on("/api/decks/draft", {"decks": []})
        text = await call_text(authed_mcp_client, "duels_list_draft_decks")
        assert "draft" in text.lower()

    async def test_draft_decks_are_listed(self, router, authed_mcp_client):
        router.json_on(
            "/api/decks/draft",
            {"draftDecks": [deck_fixtures.summary(name="Sealed pool 1", type="draft")]},
        )
        text = await call_text(authed_mcp_client, "duels_list_draft_decks")
        assert "Sealed pool 1" in text

    PACKS = [{"set": 7, "count": 3}]

    async def test_sealed_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_create_sealed", packs_config=self.PACKS)
        assert text.startswith("Error:")

    async def test_sealed_posts_the_pack_configuration(self, router, authed_mcp_client):
        router.json_on("/api/sealed/create", {"deckId": NEW_DECK_ID})
        await call_text(
            authed_mcp_client, "duels_create_sealed", packs_config=self.PACKS
        )
        assert router.calls[-1].url.path == "/api/sealed/create"
        assert b"packsConfig" in router.calls[-1].content

    async def test_playground_checks_access_before_creating(self, router, authed_mcp_client):
        """Creating without the entitlement fails server-side; asking first
        gives the caller a reason instead."""
        router.json_on("/api/playground/access", {"hasAccess": False})
        text = await call_text(
            authed_mcp_client, "duels_create_playground", seed="shift-testing-1"
        )
        assert text.startswith("Error:")
        assert not any(
            c.url.path == "/api/playground/create-from-spec" for c in router.calls
        )

    async def test_playground_creates_when_allowed(self, router, authed_mcp_client):
        router.json_on("/api/playground/access", {"hasAccess": True})
        router.json_on("/api/playground/create-from-spec", {"gameId": "pg-1"})
        text = await call_text(
            authed_mcp_client, "duels_create_playground", seed="shift-testing-1"
        )
        assert not text.startswith("Error:")
        assert any(c.url.path == "/api/playground/create-from-spec" for c in router.calls)
