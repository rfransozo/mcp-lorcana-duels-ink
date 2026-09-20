"""Unit tests for the shared parameter types.

These aliases are reused across the whole tool surface, so a constraint that
silently disappears here weakens every tool at once.
"""

import pytest
from pydantic import Field, TypeAdapter, ValidationError
from typing import Annotated, get_args

from src import models
from src.models import BotDifficulty, Limit, Offset, ResponseFormat


def constraints(alias) -> dict:
    """The JSON schema a parameter alias produces."""
    return TypeAdapter(alias).json_schema()


class TestEnums:
    def test_response_format_values(self):
        assert {f.value for f in ResponseFormat} == {"markdown", "json"}

    def test_markdown_is_the_default_everywhere(self):
        assert models.ResponseFmt.__metadata__[0].default is ResponseFormat.MARKDOWN

    def test_bot_difficulties_match_the_site(self):
        assert {d.value for d in BotDifficulty} == {"easy", "normal", "difficult"}


class TestLimit:
    def test_bounds_are_enforced(self):
        schema = constraints(Limit)
        assert schema["minimum"] == 1 and schema["maximum"] == 100

    def test_rejects_out_of_range(self):
        adapter = TypeAdapter(Limit)
        with pytest.raises(ValidationError):
            adapter.validate_python(0)
        with pytest.raises(ValidationError):
            adapter.validate_python(101)

    def test_accepts_the_edges(self):
        adapter = TypeAdapter(Limit)
        assert adapter.validate_python(1) == 1
        assert adapter.validate_python(100) == 100


class TestOffset:
    def test_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            TypeAdapter(Offset).validate_python(-1)

    def test_zero_is_the_first_page(self):
        assert TypeAdapter(Offset).validate_python(0) == 0


class TestDocumentation:
    ALIASES = [
        "ResponseFmt",
        "Limit",
        "Offset",
        "GameId",
        "TableId",
        "CardInstanceId",
        "DeckId",
        "DeckCardIds",
        "SkipTriggers",
    ]

    @pytest.mark.parametrize("name", ALIASES)
    def test_every_alias_documents_itself(self, name):
        """The contract test asserts no parameter lacks a description; these
        aliases are where most of those descriptions come from."""
        alias = getattr(models, name)
        field = next(m for m in alias.__metadata__ if isinstance(m, type(Field())))
        assert field.description and len(field.description) > 20

    def test_card_instance_id_warns_against_the_wrong_id(self):
        """Confusing instanceId with definitionId is the single easiest mistake
        to make against this API."""
        description = models.CardInstanceId.__metadata__[0].description
        assert "definitionId" in description

    def test_game_id_says_where_to_get_one(self):
        description = models.GameId.__metadata__[0].description
        assert "duels_start_bot_game" in description or "duels_list_active_games" in description
