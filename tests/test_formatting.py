"""Unit tests for the shared response-shaping helpers."""

import json

import pytest

from src.formatting import (
    as_json,
    bullet,
    empty_result,
    join_lines,
    paginate,
    pagination_footer,
    render,
)
from src.models import ResponseFormat


class TestPaginate:
    def test_middle_page_reports_more(self):
        p = paginate([{"a": 1}] * 10, total=35, offset=10, key="cards")
        assert p == {
            "total": 35,
            "count": 10,
            "offset": 10,
            "cards": [{"a": 1}] * 10,
            "has_more": True,
            "next_offset": 20,
        }

    def test_last_page_has_no_next_offset(self):
        p = paginate([{"a": 1}] * 5, total=25, offset=20, key="decks")
        assert p["has_more"] is False
        assert p["next_offset"] is None

    def test_unknown_total_is_derived_and_terminates(self):
        """The API does not always report a total; we must not invent more pages."""
        p = paginate([{"a": 1}] * 3, total=None, offset=6, key="items")
        assert p["total"] == 9
        assert p["has_more"] is False

    def test_empty_page(self):
        p = paginate([], total=0, offset=0, key="items")
        assert p["count"] == 0 and p["has_more"] is False

    def test_key_names_the_list_field(self):
        assert "friends" in paginate([], total=0, offset=0, key="friends")


class TestRender:
    def test_json_mode_ignores_the_markdown_renderer(self):
        out = render({"n": 1}, ResponseFormat.JSON, lambda p: "should not appear")
        assert json.loads(out) == {"n": 1}

    def test_markdown_mode_uses_the_renderer(self):
        assert render({"n": 1}, ResponseFormat.MARKDOWN, lambda p: f"n={p['n']}") == "n=1"

    def test_as_json_handles_non_serialisable_values(self):
        assert "datetime" in as_json({"x": object()}) or as_json({"x": 1}) == '{\n  "x": 1\n}'


class TestBullet:
    @pytest.mark.parametrize("value", [None, "", []])
    def test_empty_values_are_skipped(self, value):
        assert bullet("Label", value) == ""

    def test_lists_are_joined(self):
        assert bullet("Colors", ["emerald", "ruby"]) == "- **Colors**: emerald, ruby"

    def test_zero_is_kept(self):
        """0 is meaningful (lore, damage) and must not be dropped as falsy."""
        assert bullet("Lore", 0) == "- **Lore**: 0"

    def test_false_is_kept(self):
        assert bullet("Valid", False) == "- **Valid**: False"


class TestJoinLines:
    def test_drops_empty_fragments(self):
        assert join_lines(["a", "", "b", ""]) == "a\nb"


class TestPaginationFooter:
    def test_mentions_next_offset_when_more(self):
        footer = pagination_footer(
            {"total": 30, "count": 10, "offset": 0, "has_more": True, "next_offset": 10}
        )
        assert "offset=10" in footer

    def test_no_next_hint_on_last_page(self):
        footer = pagination_footer(
            {"total": 10, "count": 10, "offset": 0, "has_more": False, "next_offset": None}
        )
        assert "offset=" not in footer


class TestEmptyResult:
    def test_includes_the_hint(self):
        msg = empty_result("cards", "Try a broader query.")
        assert "No cards found." in msg and "Try a broader query." in msg

    def test_works_without_a_hint(self):
        assert empty_result("decks") == "No decks found."
