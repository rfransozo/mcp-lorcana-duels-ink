"""Opt-in smoke tests against the real duels.ink.

Deselected by default (see the `live` marker in pyproject.toml). Run with:

    pytest -m live

These only read, only touch anonymous endpoints, and never create a game or
use an account - duels.ink is a fan-made site run by the community and the
suite should not generate load on it.
"""

import re

import pytest

from src.cards import CardCatalog
from src.client import DuelsClient

pytestmark = pytest.mark.live

# The build the protocol notes were written against. A mismatch is not a
# failure - it is a prompt to re-check docs/PROTOCOL.md.
DOCUMENTED_BUILD_ID = "caee107"


@pytest.fixture
async def live_client():
    client = DuelsClient(cookie=None)
    yield client
    await client.aclose()


class TestPublicEndpoints:
    async def test_card_catalog_shape_is_unchanged(self, live_client):
        data = await live_client.get("/api/cards", params={"limit": 5}, authed=False)
        assert set(data) >= {"meta", "cards"}
        assert set(data["meta"]) >= {"total", "limit", "offset", "hasMore"}
        assert data["meta"]["total"] > 3000

    async def test_catalog_ids_still_match_the_game_format(self, live_client):
        """The catalog id doubles as the engine's definitionId. If that ever
        diverges, every in-game card lookup breaks."""
        data = await live_client.get("/api/cards", params={"limit": 100}, authed=False)
        ids = [c["id"] for c in data["cards"]]
        assert sum(bool(re.fullmatch(r"\d{1,2}-\d{1,3}", i)) for i in ids) > 80

    async def test_card_fields_used_by_the_renderer_are_present(self, live_client):
        data = await live_client.get(
            "/api/cards", params={"q": "Flotsam", "limit": 5}, authed=False
        )
        card = data["cards"][0]
        assert set(card) >= {"id", "fullName", "type", "cost", "inkable", "colors"}

    async def test_search_and_set_filters_still_work(self, live_client):
        catalog = CardCatalog(live_client)
        rows, total = await catalog.search(query="Flotsam", limit=3)
        assert total >= 1 and rows

    async def test_public_decks_are_readable_without_an_account(self, live_client):
        data = await live_client.get("/api/decks/public", authed=False)
        assert data["decks"], "no public decks returned"

    async def test_public_settings(self, live_client):
        data = await live_client.get("/api/settings/public", authed=False)
        assert "settings" in data


class TestAuthBoundary:
    async def test_account_endpoints_still_require_the_cookie(self, live_client):
        """If this stops being true, the auth model changed."""
        from src.client import DuelsError

        with pytest.raises(DuelsError, match="Not authenticated"):
            await live_client.get("/api/decks")


class TestDrift:
    async def test_report_the_current_build(self, live_client):
        build_id = await live_client.build_id()
        assert build_id, "/api/version stopped reporting a buildId"
        if build_id != DOCUMENTED_BUILD_ID:
            pytest.skip(
                f"duels.ink is on build {build_id}, docs/PROTOCOL.md documents "
                f"{DOCUMENTED_BUILD_ID}. Re-verify the protocol notes."
            )
