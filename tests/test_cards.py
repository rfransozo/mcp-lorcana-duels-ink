"""Unit tests for the card catalog."""

import pytest

from src.cards import CardCatalog, parse_set_number, summarise
from tests.fixtures import cards as card_fixtures


class TestParseSetNumber:
    @pytest.mark.parametrize(
        ("definition_id", "expected"),
        [
            ("10-71", 10),
            ("5-195", 5),
            ("13-12", 13),
            ("4-DIS-1", 4),   # promo ids still start with the set
            ("weird", None),
            ("", None),
        ],
    )
    def test_extracts_the_leading_set(self, definition_id, expected):
        assert parse_set_number(definition_id) == expected


class TestSummarise:
    def test_keeps_the_fields_that_matter_for_play(self):
        s = summarise(card_fixtures.BY_ID["10-71"])
        assert s["id"] == "10-71"
        assert s["fullName"] == "Flotsam - Slippery as an Eel"
        assert s["cost"] == 3 and s["strength"] == 4 and s["willpower"] == 2

    def test_drops_empty_fields_to_save_context(self):
        s = summarise(card_fixtures.BY_ID["12-133"])
        assert "strength" not in s   # actions have none
        assert "subtypes" not in s   # empty list

    def test_drops_market_and_image_noise(self):
        s = summarise(card_fixtures.BY_ID["10-71"])
        assert "imageUrl" not in s and "flavorText" not in s


class TestLookup:
    async def test_get_by_id(self, catalog):
        card = await catalog.get("10-71")
        assert card["fullName"] == "Flotsam - Slippery as an Eel"

    async def test_get_only_loads_the_one_set_needed(self, catalog, router):
        """Loading all 32 catalog pages for a single id would be wasteful; the
        set encoded in the id narrows it to a couple of requests."""
        await catalog.get("10-71")
        assert router.count("/api/cards") >= 1
        for request in router.calls:
            if request.url.path == "/api/cards":
                assert request.url.params.get("set") == "10"

    async def test_unknown_id_returns_none(self, catalog):
        assert await catalog.get("99-999") is None

    async def test_second_lookup_is_served_from_cache(self, catalog, router):
        await catalog.get("10-71")
        before = router.count("/api/cards")
        await catalog.get("10-71")
        assert router.count("/api/cards") == before

    async def test_name_of_falls_back_to_the_raw_id(self, catalog):
        assert await catalog.name_of("99-999") == "99-999"

    async def test_name_of_resolves(self, catalog):
        assert await catalog.name_of("5-195") == "Pete - Games Referee"


class TestResolveMany:
    async def test_resolves_a_batch(self, catalog):
        resolved = await catalog.resolve_many(["10-71", "11-97", "12-133"])
        assert resolved["10-71"]["fullName"] == "Flotsam - Slippery as an Eel"
        assert resolved["11-97"]["fullName"] == "Education or Elimination"

    async def test_only_fetches_the_sets_involved(self, catalog, router):
        await catalog.resolve_many(["10-71", "10-45"])
        sets = {
            r.url.params.get("set") for r in router.calls if r.url.path == "/api/cards"
        }
        assert sets == {"10"}

    async def test_unknown_ids_come_back_as_none_not_dropped(self, catalog):
        resolved = await catalog.resolve_many(["10-71", "99-999"])
        assert set(resolved) == {"10-71", "99-999"}
        assert resolved["99-999"] is None

    async def test_unparseable_id_falls_back_to_a_full_load(self, catalog, router):
        await catalog.resolve_many(["garbage"])
        unfiltered = [
            r for r in router.calls
            if r.url.path == "/api/cards" and "set" not in r.url.params
        ]
        assert unfiltered, "expected a full catalog load when an id has no set"


class TestSearch:
    async def test_text_query_is_pushed_to_the_api(self, catalog, router):
        rows, total = await catalog.search(query="Flotsam", limit=5)
        assert total == 1 and rows[0]["id"] == "10-71"
        assert router.calls[-1].url.params["q"] == "Flotsam"

    async def test_pagination_is_delegated_when_there_are_no_local_filters(
        self, catalog, router
    ):
        await catalog.search(limit=2, offset=2)
        params = router.calls[-1].url.params
        assert params["limit"] == "2" and params["offset"] == "2"

    @pytest.mark.parametrize(
        ("kwargs", "expected_ids"),
        [
            ({"card_type": "action"}, {"11-97", "12-133"}),
            ({"card_type": "location"}, {"13-12"}),
            ({"color": "ruby"}, {"12-133", "10-103"}),
            ({"cost": 3}, {"10-71", "10-45", "5-195", "13-12"}),
            ({"inkable": False}, {"10-103"}),
            ({"rarity": "uncommon"}, {"11-97", "5-195", "12-77"}),
        ],
    )
    async def test_local_filters(self, catalog, kwargs, expected_ids):
        rows, _ = await catalog.search(limit=100, **kwargs)
        assert {r["id"] for r in rows} == expected_ids

    async def test_filters_combine_with_and(self, catalog):
        rows, _ = await catalog.search(card_type="character", color="emerald", limit=100)
        assert {r["id"] for r in rows} == {"10-71", "13-80", "12-77"}

    async def test_filters_are_case_insensitive(self, catalog):
        rows, _ = await catalog.search(color="EMERALD", card_type="Character", limit=100)
        assert {r["id"] for r in rows} == {"10-71", "13-80", "12-77"}

    async def test_total_reflects_matches_not_the_page(self, catalog):
        rows, total = await catalog.search(card_type="character", limit=1)
        assert len(rows) == 1 and total > 1

    async def test_no_match_returns_empty(self, catalog):
        rows, total = await catalog.search(query="Nonexistent Card", limit=5)
        assert rows == [] and total == 0
