"""The Coconut pool that ships beside the server."""

import pytest

from src import coconuts


@pytest.fixture
def pool_at(monkeypatch):
    """Point the pool at another file, with the cache cleared either side."""

    def point(path):
        monkeypatch.setattr(coconuts, "POOL_PATH", path)
        coconuts.pool.cache_clear()

    yield point
    coconuts.pool.cache_clear()


class TestRecord:
    def test_a_coconut_reads_like_a_catalog_card(self):
        record = coconuts.record("coconut-011")
        assert record["fullName"] == "Mr. Incredible - Super Strong"
        assert record["type"] == "coconut"
        assert record["rulesText"]

    def test_a_coconut_newer_than_the_pool_falls_through(self):
        """The site adds Coconuts. Until the pool is refreshed, an unknown one
        goes on to the catalog rather than posing as a card we know."""
        assert coconuts.record("coconut-999") is None

    def test_an_ordinary_card_is_not_a_coconut(self):
        assert coconuts.record("10-71") is None
        assert coconuts.record(None) is None


class TestPool:
    def test_every_shipped_coconut_has_a_name_and_rules(self):
        """What scripts/refresh_coconuts.py writes, the renderer can read."""
        pool = coconuts.pool()
        assert pool
        for coconut_id, entry in pool.items():
            assert coconut_id.startswith("coconut-")
            assert entry["name"] and entry["text"], coconut_id

    def test_a_missing_pool_names_nothing_instead_of_failing(self, pool_at, tmp_path):
        pool_at(tmp_path / "absent.json")
        assert coconuts.pool() == {}
        assert coconuts.record("coconut-011") is None

    @pytest.mark.parametrize("content", ["{not json", "[]"])
    def test_a_damaged_pool_names_nothing_instead_of_failing(
        self, pool_at, tmp_path, content
    ):
        damaged = tmp_path / "coconuts.json"
        damaged.write_text(content, encoding="utf-8")
        pool_at(damaged)
        assert coconuts.pool() == {}
